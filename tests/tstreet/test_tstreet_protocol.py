# SPDX-License-Identifier: MIT
"""Comprehensive test suite for T-Street Gnosis & Deal ledger, token, Safe, deals, and HTTP control plane."""

from __future__ import annotations

import json
import threading
from urllib.request import Request, urlopen

import pytest

from tstreet_ledger.control_plane import ControlSupervisor
from tstreet_ledger.engine import TStreetProtocolEngine
from tstreet_ledger.gnosis_safe import build_eip712_safe_tx_payload
from tstreet_ledger.ledger import (
    build_merkle_proof,
    compute_merkle_root,
    verify_merkle_proof,
)
from tstreet_ledger.server import create_http_server
from tstreet_ledger.units import (
    GNOSIS_CHAIN_ID,
    ONE_TST,
    derive_address,
    format_wei_to_tst,
    normalize_address,
    parse_tst_to_wei,
)


def test_units_exact_18_decimal_conversion() -> None:
    assert parse_tst_to_wei("1") == ONE_TST
    assert parse_tst_to_wei("0.000000000000000001") == 1
    assert parse_tst_to_wei("125000.25") == 125_000 * ONE_TST + (ONE_TST // 4)
    assert format_wei_to_tst(125_000 * ONE_TST + (ONE_TST // 4)) == "125,000.25"

    with pytest.raises(ValueError):
        parse_tst_to_wei("-5")
    with pytest.raises(ValueError):
        parse_tst_to_wei("0.0000000000000000001")  # 19 decimals rejected

    addr = derive_address("unit-test-account")
    assert normalize_address(addr.upper().replace("0X", "0x")) == addr


def test_deterministic_mempool_priority_and_causal_nonce_ordering() -> None:
    engine = TStreetProtocolEngine(seed_demo_state=False)

    # Submit 3 transfers from distinct desks in LOW -> HIGH -> MEDIUM fee arrival order
    t_low = engine.submit_transfer(
        sender=engine.desk_alpha,
        recipient=engine.desk_gnosis,
        amount_tst="100",
        priority_fee_tst="0.1",
        seal_immediately=False,
    )["tx"]
    t_high = engine.submit_transfer(
        sender=engine.market_maker,
        recipient=engine.desk_alpha,
        amount_tst="200",
        priority_fee_tst="5.0",
        seal_immediately=False,
    )["tx"]
    t_med = engine.submit_transfer(
        sender=engine.desk_gnosis,
        recipient=engine.market_maker,
        amount_tst="150",
        priority_fee_tst="1.25",
        seal_immediately=False,
    )["tx"]

    # Also enqueue a second sequential tx from desk_alpha with a HUGE priority fee (10.0 TST):
    # causal per-sender nonce ordering MUST keep nonce N+1 behind desk_alpha's nonce N!
    t_alpha_second = engine.submit_transfer(
        sender=engine.desk_alpha,
        recipient=engine.market_maker,
        amount_tst="50",
        priority_fee_tst="10.0",
        seal_immediately=False,
    )["tx"]

    ordered = engine.ledger.preview_ordered_mempool()
    hashes = [tx.tx_hash for tx in ordered]
    assert hashes == [
        t_high["tx_hash"],  # 5.0 TST priority fee
        t_med["tx_hash"],  # 1.25 TST priority fee
        t_low["tx_hash"],  # 0.1 TST priority fee (unlocks desk_alpha's next nonce)
        t_alpha_second["tx_hash"],  # 10.0 TST priority fee, sequenced immediately after t_low!
    ]

    sealed = engine.seal_pending_block()
    assert sealed is not None
    assert [tx.tx_hash for tx in sealed.transactions] == hashes
    assert engine.check_invariants()["ok"] is True


def test_merkle_proof_verification_and_tamper_detection() -> None:
    tx_hashes = [
        "0x" + f"{i:064x}"
        for i in range(1, 8)  # odd leaf count (7) tests odd-node duplication across tree levels
    ]
    root = compute_merkle_root(tx_hashes)
    for idx, h in enumerate(tx_hashes):
        proof = build_merkle_proof(tx_hashes, idx)
        assert verify_merkle_proof(h, proof, root) is True
        tampered = "0x" + ("f" * 64)
        assert verify_merkle_proof(tampered, proof, root) is False


def test_deal_escrow_happy_path_and_safe_dispute_arbitration() -> None:
    engine = TStreetProtocolEngine(seed_demo_state=False)

    # 1. Happy path: create -> fund -> deliver -> buyer release
    deal = engine.create_deal(
        buyer=engine.desk_alpha,
        seller=engine.desk_gnosis,
        amount_tst="10000",
        title="OTC Settlement #1",
        terms_summary="10k TST atomic settlement",
    )
    assert deal["state"] == "OPEN"

    funded = engine.fund_deal(deal_id=deal["deal_id"], caller=engine.desk_alpha)
    assert funded["state"] == "FUNDED"
    assert funded["escrow_locked_tst"] == "10,000"
    assert engine.check_invariants()["ok"] is True

    delivered = engine.deliver_deal(deal_id=deal["deal_id"], caller=engine.desk_gnosis)
    assert delivered["state"] == "DELIVERED"

    seller_before = int(engine.get_wallet(engine.desk_gnosis)["balance_wei"])
    treasury_before = int(engine.get_wallet(engine.safe_address)["balance_wei"])
    released = engine.release_deal(deal_id=deal["deal_id"], caller=engine.desk_alpha)
    assert released["state"] == "RELEASED"

    expected_fee = (10_000 * ONE_TST * engine.deal_fee_bps) // 10_000  # 25 TST at 25 bps
    expected_seller = 10_000 * ONE_TST - expected_fee
    assert int(engine.get_wallet(engine.desk_gnosis)["balance_wei"]) - seller_before == expected_seller
    assert int(engine.get_wallet(engine.safe_address)["balance_wei"]) - treasury_before == expected_fee
    assert engine.check_invariants()["ok"] is True

    # 2. Dispute path: 2-of-3 Gnosis Safe resolves 60% seller / 40% buyer
    d2 = engine.create_deal(
        buyer=engine.desk_alpha,
        seller=engine.market_maker,
        amount_tst="20000",
        title="Disputed OTC Swap",
    )
    engine.fund_deal(deal_id=d2["deal_id"], caller=engine.desk_alpha)
    disputed = engine.dispute_deal(
        deal_id=d2["deal_id"],
        caller=engine.market_maker,
        reason="Milestone partial completion",
    )
    assert disputed["state"] == "DISPUTED"

    prop = engine.propose_safe_action(
        proposer=engine.ledger_signer_1,
        action="RESOLVE_DEAL",
        title="Arbitrate 60/40 split",
        params={"deal_id": d2["deal_id"], "seller_share_bps": 6000},
    )
    assert prop["status"] == "PENDING"
    assert prop["approval_count"] == 1

    # Duplicate signature by same Ledger owner must fail
    with pytest.raises(ValueError, match="already approved"):
        engine.approve_safe_proposal(proposal_id=prop["proposal_id"], signer=engine.ledger_signer_1)

    # Second Ledger hardware owner co-signs -> hits 2/3 threshold & executes on-ledger!
    executed = engine.approve_safe_proposal(proposal_id=prop["proposal_id"], signer=engine.ledger_signer_2)
    assert executed["status"] == "EXECUTED"
    assert executed["execution_tx_hash"].startswith("0x")
    assert engine.check_invariants()["ok"] is True


def test_gnosis_safe_mint_cap_and_eip712_payload() -> None:
    engine = TStreetProtocolEngine(seed_demo_state=False)

    # Exceeding 100M TST cap must be rejected at proposal time
    with pytest.raises(ValueError, match="hard cap"):
        engine.propose_safe_action(
            proposer=engine.ledger_signer_1,
            action="SAFE_MINT",
            title="Oversized mint",
            params={"recipient": engine.desk_alpha, "amount_tst": "90000000"},
        )

    # Valid 500,000 TST mint via 2-of-3 Ledger signers
    prop = engine.propose_safe_action(
        proposer=engine.ledger_signer_1,
        action="SAFE_MINT",
        title="Mint 500k TST expansion tranche",
        params={"recipient": engine.desk_alpha, "amount_tst": "500000"},
    )
    assert prop["eip712_payload"]["domain"]["chainId"] == GNOSIS_CHAIN_ID
    assert prop["eip712_payload"]["primaryType"] == "SafeTx"

    # EIP-712 digest must be deterministic
    recomputed = build_eip712_safe_tx_payload(
        safe_address=engine.safe_address,
        target_contract=engine.token_contract,
        safe_nonce=prop["safe_nonce"],
        action="SAFE_MINT",
        params=prop["params"],
    )
    assert recomputed["meta"]["eip712SigningDigest"] == prop["eip712_payload"]["meta"]["eip712SigningDigest"]

    before_supply = engine.total_supply_wei
    engine.approve_safe_proposal(proposal_id=prop["proposal_id"], signer=engine.ledger_signer_3)
    assert engine.total_supply_wei == before_supply + 500_000 * ONE_TST
    assert engine.check_invariants()["ok"] is True


def test_control_supervisor_auto_sweep_and_watchdog_circuit_breaker() -> None:
    engine = TStreetProtocolEngine(seed_demo_state=False)
    supervisor = ControlSupervisor(engine, tick_interval_sec=0.5)

    # Create & fund a short-lived deal, then advance sweep clock past expiry
    deal = engine.create_deal(
        buyer=engine.desk_alpha,
        seller=engine.desk_gnosis,
        amount_tst="1000",
        title="Expiring Deal",
        expires_in_seconds=5,
    )
    funded = engine.fund_deal(deal_id=deal["deal_id"], caller=engine.desk_alpha)
    refunded = engine.sweep_expired_deals(now_ms=funded["expires_ms"] + 1)
    assert refunded == [deal["deal_id"]]
    assert engine.check_invariants()["ok"] is True

    # Enqueue a transfer and verify run_single_tick auto-sequences it into a block
    engine.submit_transfer(
        sender=engine.desk_alpha,
        recipient=engine.desk_gnosis,
        amount_tst="250",
        priority_fee_tst="0.5",
        seal_immediately=False,
    )
    tick_res = supervisor.run_single_tick()
    assert tick_res["sealed_block_height"] is not None
    assert tick_res["invariants_ok"] is True

    # Simulate unexpected balance corruption -> watchdog MUST engage circuit breaker automatically
    engine._balances[engine.desk_alpha] += 1  # unbacked 1 wei breaks supply conservation
    corrupt_tick = supervisor.run_single_tick()
    assert corrupt_tick["invariants_ok"] is False
    assert engine.circuit_breaker_paused is True
    assert "Watchdog invariant trip" in engine.circuit_breaker_reason


def test_http_control_plane_endpoints_end_to_end() -> None:
    server, ctx = create_http_server(host="127.0.0.1", port=0, start_daemon=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"

    try:
        # 1. Dashboard HTML loads
        with urlopen(f"{base}/") as resp:
            html = resp.read().decode("utf-8")
            assert resp.status == 200
            assert "T-Street Gnosis &amp; Deal" in html

        # 2. Health & State
        with urlopen(f"{base}/api/health") as resp:
            health = json.loads(resp.read().decode("utf-8"))
            assert health["ok"] is True

        # 3. Priority burst + Seal block + Merkle proof lookup
        req = Request(
            f"{base}/api/sequencer/demo-batch",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            burst = json.loads(resp.read().decode("utf-8"))["result"]
            ordered_fees = [t["priority_fee_tst"] for t in burst["deterministic_execution_order"]]
            assert ordered_fees == ["2.5", "0.5", "0.05"]

        seal_req = Request(
            f"{base}/api/sequencer/seal",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(seal_req) as resp:
            sealed = json.loads(resp.read().decode("utf-8"))["result"]
            assert sealed["sealed"] is True
            tx_hash = sealed["block"]["transactions"][0]["tx_hash"]

        with urlopen(f"{base}/api/tx/{tx_hash}") as resp:
            proof_data = json.loads(resp.read().decode("utf-8"))["result"]
            assert proof_data["status"] == "CONFIRMED"
            assert proof_data["merkle_verified"] is True

        # 4. Solidity contracts endpoint
        with urlopen(f"{base}/api/contracts") as resp:
            contracts = json.loads(resp.read().decode("utf-8"))["contracts"]
            assert "contract TStreetToken" in contracts["TStreetToken.sol"]
            assert "contract TStreetDealEscrow" in contracts["TStreetDealEscrow.sol"]
    finally:
        ctx.shutdown()
        server.shutdown()
        server.server_close()


def test_solidity_contracts_security_primitives_present() -> None:
    engine = TStreetProtocolEngine(seed_demo_state=False)
    contracts = engine.load_contracts_source()

    token_sol = contracts["TStreetToken.sol"]
    assert "pragma solidity ^0.8.24;" in token_sol
    assert "MAX_SUPPLY = 100_000_000 * 1e18" in token_sol
    assert "onlySafeGovernor" in token_sol
    assert "function permit(" in token_sol

    escrow_sol = contracts["TStreetDealEscrow.sol"]
    assert "pragma solidity ^0.8.24;" in escrow_sol
    assert "nonReentrant" in escrow_sol
    assert "resolveDisputeBySafe" in escrow_sol
    assert "claimExpiredRefund" in escrow_sol
