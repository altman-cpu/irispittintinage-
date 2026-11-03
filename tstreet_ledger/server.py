# SPDX-License-Identifier: MIT
"""HTTP Control Plane API & Live Dashboard server for T-Street Gnosis & Deal."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from tstreet_ledger.control_plane import ControlSupervisor
from tstreet_ledger.engine import TStreetProtocolEngine

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 1_048_576  # 1 MiB


class TStreetAppContext:
    """Shared server state bundling the protocol engine and continuous control supervisor."""

    def __init__(self, *, start_daemon: bool = True, tick_interval_sec: float = 3.0) -> None:
        self.engine = TStreetProtocolEngine(seed_demo_state=True)
        self.supervisor = ControlSupervisor(
            self.engine,
            tick_interval_sec=tick_interval_sec,
            auto_sequence_enabled=True,
            auto_expire_deals_enabled=True,
        )
        if start_daemon:
            self.supervisor.start()

    def shutdown(self) -> None:
        self.supervisor.stop()


def make_handler(ctx: TStreetAppContext) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to `ctx`."""

    class ControlPlaneHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep server stdout concise while surfacing non-polling mutations/errors
            msg = fmt % args
            if "GET /api/state" not in msg and "GET /api/health" not in msg:
                print(f"[tstreet-server] {self.address_string()} - {msg}", flush=True)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(raw)

        def _send_static(self, filename: str) -> None:
            safe_path = (STATIC_DIR / filename).resolve()
            if not str(safe_path).startswith(str(STATIC_DIR.resolve())) or not safe_path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Asset not found"})
                return
            data = safe_path.read_bytes()
            ctype, _ = mimetypes.guess_type(str(safe_path))
            if safe_path.suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            elif safe_path.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif safe_path.suffix == ".html":
                ctype = "text/html; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self) -> dict[str, Any]:
            length_str = self.headers.get("Content-Length", "0")
            length = int(length_str) if length_str.isdigit() else 0
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise ValueError("Request body exceeds 1 MiB limit")
            raw = self.rfile.read(length)
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)

            if path in ("/", "/index.html"):
                self._send_static("index.html")
                return
            if path.startswith("/static/"):
                rel = path.removeprefix("/static/")
                self._send_static(rel)
                return

            if path == "/api/health":
                inv = ctx.engine.check_invariants()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": inv["ok"],
                        "service": "tstreet-gnosis-deal-control-plane",
                        "control": ctx.supervisor.status(),
                        "invariants": inv,
                    },
                )
                return

            if path == "/api/state":
                snap = ctx.engine.snapshot()
                snap["control"] = ctx.supervisor.status()
                self._send_json(HTTPStatus.OK, {"ok": True, "data": snap})
                return

            if path == "/api/contracts":
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "contracts": ctx.engine.load_contracts_source()},
                )
                return

            if path.startswith("/api/tx/"):
                tx_hash = path.removeprefix("/api/tx/").strip()
                found = ctx.engine.ledger.lookup_transaction(tx_hash)
                if found is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Transaction not found: {tx_hash}"})
                else:
                    self._send_json(HTTPStatus.OK, {"ok": True, "result": found})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Unknown route: {path}"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            try:
                body = self._read_json_body()
                result = self._dispatch_post(path, body)
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
            except KeyError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
            except (ValueError, RuntimeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive API error boundary
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

        def _dispatch_post(self, path: str, body: dict[str, Any]) -> Any:
            if path == "/api/wallets/register":
                wallet = ctx.engine.register_wallet(
                    label=str(body.get("label", "")),
                    address=body.get("address") or None,
                )
                ctx.supervisor.record_event("WALLET_REGISTERED", f"Registered wallet '{wallet['label']}'")
                return wallet

            if path == "/api/transfers":
                res = ctx.engine.submit_transfer(
                    sender=str(body["sender"]),
                    recipient=str(body["recipient"]),
                    amount_tst=body["amount_tst"],
                    priority_fee_tst=body.get("priority_fee_tst", "0"),
                    memo=str(body.get("memo", "")),
                    seal_immediately=bool(body.get("seal_immediately", False)),
                )
                tx = res["tx"]
                ctx.supervisor.record_event(
                    "TX_QUEUED",
                    f"Transfer {tx['amount_tst']} TST queued (prio fee {tx['priority_fee_tst']} TST, "
                    f"hash {tx['tx_hash'][:12]}...)",
                )
                return res

            if path == "/api/burns":
                res = ctx.engine.submit_burn(
                    sender=str(body["sender"]),
                    amount_tst=body["amount_tst"],
                    priority_fee_tst=body.get("priority_fee_tst", "0"),
                    reason=str(body.get("reason", "")),
                    seal_immediately=bool(body.get("seal_immediately", True)),
                )
                ctx.supervisor.record_event("TST_BURN", f"Burned {res['tx']['amount_tst']} TST")
                return res

            if path == "/api/sequencer/seal":
                block = ctx.engine.seal_pending_block(allow_empty=bool(body.get("allow_empty", False)))
                if block is not None:
                    ctx.supervisor.record_event(
                        "MANUAL_SEAL",
                        f"Operator sealed Block #{block.height} ({len(block.transactions)} tx)",
                    )
                return {"sealed": block is not None, "block": block.to_dict() if block else None}

            if path == "/api/sequencer/demo-batch":
                # Enqueue 3 competing transfers from distinct accounts in LOW -> HIGH -> MEDIUM arrival order
                # so the operator can observe deterministic priority-fee reordering in the mempool!
                e = ctx.engine
                t1 = e.submit_transfer(
                    sender=e.desk_alpha,
                    recipient=e.desk_gnosis,
                    amount_tst="250",
                    priority_fee_tst="0.05",
                    memo="Batch #1 (arrived 1st, low 0.05 TST priority fee)",
                    seal_immediately=False,
                )["tx"]
                t2 = e.submit_transfer(
                    sender=e.market_maker,
                    recipient=e.desk_alpha,
                    amount_tst="500",
                    priority_fee_tst="2.5",
                    memo="Batch #2 (arrived 2nd, HIGH 2.50 TST priority fee -> jumps to #1)",
                    seal_immediately=False,
                )["tx"]
                t3 = e.submit_transfer(
                    sender=e.desk_gnosis,
                    recipient=e.market_maker,
                    amount_tst="175",
                    priority_fee_tst="0.5",
                    memo="Batch #3 (arrived 3rd, MED 0.50 TST priority fee -> ordered #2)",
                    seal_immediately=False,
                )["tx"]
                ctx.supervisor.record_event(
                    "PRIORITY_BURST",
                    "Enqueued 3 competing transfers (0.05, 2.50, 0.50 TST priority fees) into ordered mempool.",
                )
                return {
                    "submitted_arrival_order": [t1, t2, t3],
                    "deterministic_execution_order": [tx.to_dict() for tx in e.ledger.preview_ordered_mempool()],
                }

            if path == "/api/deals/create":
                deal = ctx.engine.create_deal(
                    buyer=str(body["buyer"]),
                    seller=str(body["seller"]),
                    amount_tst=body["amount_tst"],
                    title=str(body["title"]),
                    terms_summary=str(body.get("terms_summary", "")),
                    expires_in_seconds=int(body.get("expires_in_seconds", 86_400)),
                    seal_immediately=True,
                )
                ctx.supervisor.record_event(
                    "DEAL_CREATED",
                    f"Created {deal['deal_id']} ({deal['amount_tst']} TST): {deal['title']}",
                )
                return deal

            if path == "/api/deals/action":
                action = str(body["action"]).lower()
                deal_id = str(body["deal_id"])
                caller = str(body["caller"])
                if action == "fund":
                    res = ctx.engine.fund_deal(deal_id=deal_id, caller=caller, seal_immediately=True)
                elif action == "deliver":
                    res = ctx.engine.deliver_deal(
                        deal_id=deal_id,
                        caller=caller,
                        delivery_note=str(body.get("note", "")),
                        seal_immediately=True,
                    )
                elif action == "release":
                    res = ctx.engine.release_deal(deal_id=deal_id, caller=caller, seal_immediately=True)
                elif action == "dispute":
                    res = ctx.engine.dispute_deal(
                        deal_id=deal_id,
                        caller=caller,
                        reason=str(body.get("reason", "Escalated to Gnosis Safe")),
                        seal_immediately=True,
                    )
                else:
                    raise ValueError(f"Unsupported deal action: {action}")
                ctx.supervisor.record_event("DEAL_UPDATE", f"{deal_id} -> {res['state']} (action={action})")
                return res

            if path == "/api/safe/propose":
                prop = ctx.engine.propose_safe_action(
                    proposer=str(body["proposer"]),
                    action=str(body["action"]),
                    title=str(body.get("title", "")),
                    params=dict(body.get("params", {})),
                )
                ctx.supervisor.record_event(
                    "SAFE_PROPOSAL",
                    f"Gnosis Safe {prop['proposal_id']} proposed ({prop['action']})",
                )
                return prop

            if path == "/api/safe/approve":
                prop = ctx.engine.approve_safe_proposal(
                    proposal_id=str(body["proposal_id"]),
                    signer=str(body["signer"]),
                )
                ctx.supervisor.record_event(
                    "SAFE_SIGNATURE",
                    f"{prop['proposal_id']} signed ({prop['approval_count']}/{prop['threshold']}) -> {prop['status']}",
                )
                return prop

            if path == "/api/control/config":
                if "circuit_breaker_paused" in body:
                    paused = bool(body["circuit_breaker_paused"])
                    reason = str(body.get("reason", "Operator emergency toggle"))
                    ctx.engine.set_circuit_breaker(paused, reason=reason)
                    ctx.supervisor.record_event(
                        "CIRCUIT_BREAKER",
                        f"Emergency circuit breaker {'ENGAGED' if paused else 'CLEARED'}",
                        level="WARN" if paused else "INFO",
                    )
                if body.get("run_tick_now"):
                    ctx.supervisor.run_single_tick()
                status = ctx.supervisor.configure(
                    auto_sequence_enabled=body.get("auto_sequence_enabled"),
                    auto_expire_deals_enabled=body.get("auto_expire_deals_enabled"),
                    tick_interval_sec=body.get("tick_interval_sec"),
                )
                return status

            raise ValueError(f"Unknown POST endpoint: {path}")

    return ControlPlaneHandler


def create_http_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    *,
    start_daemon: bool = True,
    tick_interval_sec: float = 3.0,
) -> tuple[ThreadingHTTPServer, TStreetAppContext]:
    ctx = TStreetAppContext(start_daemon=start_daemon, tick_interval_sec=tick_interval_sec)
    handler_cls = make_handler(ctx)
    server = ThreadingHTTPServer((host, port), handler_cls)
    return server, ctx


def main() -> None:
    parser = argparse.ArgumentParser(description="T-Street Gnosis & Deal Continuous Control Plane Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    parser.add_argument("--tick-sec", type=float, default=3.0, help="Autonomous control loop interval in seconds")
    args = parser.parse_args()

    server, ctx = create_http_server(
        host=args.host,
        port=args.port,
        start_daemon=True,
        tick_interval_sec=args.tick_sec,
    )
    actual_host, actual_port = server.server_address
    print(
        f"T-Street Gnosis & Deal Control Plane listening on http://{actual_host}:{actual_port} "
        f"(Gnosis Chain ID 100 profile, tick={args.tick_sec:.1f}s)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
