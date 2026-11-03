# SPDX-License-Identifier: MIT
"""Continuous self-hosted control plane, autonomous sequencer loop, and invariant watchdog."""

from __future__ import annotations

import collections
import threading
import time
from typing import Any

from tstreet_ledger.engine import TStreetProtocolEngine


class ControlSupervisor:
    """
    Continuous background control loop for the T-Street Gnosis & Deal ledger.

    Responsibilities on each autonomous tick:
      1. Sweep expired escrow deals (`sweep_expired_deals`).
      2. Deterministically order and seal pending mempool transactions (`seal_pending_block`)
         when `auto_sequence_enabled` is active and circuit breaker is not tripped.
      3. Run full cryptographic chain & supply conservation verification (`check_invariants`),
         automatically tripping the emergency circuit breaker if any invariant ever fails.
    """

    def __init__(
        self,
        engine: TStreetProtocolEngine,
        *,
        tick_interval_sec: float = 3.0,
        auto_sequence_enabled: bool = True,
        auto_expire_deals_enabled: bool = True,
    ) -> None:
        self.engine = engine
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

        self.tick_interval_sec = max(0.2, min(float(tick_interval_sec), 60.0))
        self.auto_sequence_enabled = bool(auto_sequence_enabled)
        self.auto_expire_deals_enabled = bool(auto_expire_deals_enabled)

        self.started_at_ms: int = int(time.time() * 1000)
        self.tick_count: int = 0
        self.blocks_auto_sealed: int = 0
        self.deals_auto_refunded: int = 0
        self.last_tick_ms: int = 0
        self.last_invariants: dict[str, Any] = self.engine.check_invariants()
        self._audit_log: collections.deque[dict[str, Any]] = collections.deque(maxlen=120)

        self.record_event(
            "CONTROL_INIT",
            "Continuous Control Supervisor initialized (Gnosis Chain ID 100 profile).",
        )

    def record_event(self, event_type: str, message: str, level: str = "INFO") -> None:
        with self._lock:
            self._audit_log.appendleft(
                {
                    "timestamp_ms": int(time.time() * 1000),
                    "level": level,
                    "event_type": event_type,
                    "message": message,
                    "tick": self.tick_count,
                }
            )

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="tstreet-control-supervisor",
                daemon=True,
            )
            self._thread.start()
            self.record_event("DAEMON_STARTED", f"Autonomous control loop active (every {self.tick_interval_sec:.1f}s)")

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self.record_event("DAEMON_STOPPED", "Autonomous control loop stopped cleanly.")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def configure(
        self,
        *,
        auto_sequence_enabled: bool | None = None,
        auto_expire_deals_enabled: bool | None = None,
        tick_interval_sec: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if auto_sequence_enabled is not None:
                self.auto_sequence_enabled = bool(auto_sequence_enabled)
                state_str = "ENABLED" if self.auto_sequence_enabled else "PAUSED"
                self.record_event("SEQUENCER_MODE", f"Automatic mempool block sequencing {state_str}")
            if auto_expire_deals_enabled is not None:
                self.auto_expire_deals_enabled = bool(auto_expire_deals_enabled)
            if tick_interval_sec is not None:
                val = float(tick_interval_sec)
                if val < 0.2 or val > 60.0:
                    raise ValueError("tick_interval_sec must be between 0.2 and 60.0 seconds")
                self.tick_interval_sec = val
                self.record_event("CADENCE_UPDATED", f"Control tick interval set to {self.tick_interval_sec:.1f}s")
            self._wake_event.set()
            return self.status()

    def run_single_tick(self) -> dict[str, Any]:
        """Execute one deterministic control, sequencing, and invariant-check cycle."""
        with self._lock:
            self.tick_count += 1
            self.last_tick_ms = int(time.time() * 1000)

            if self.auto_expire_deals_enabled and not self.engine.circuit_breaker_paused:
                expired = self.engine.sweep_expired_deals(now_ms=self.last_tick_ms)
                if expired:
                    self.deals_auto_refunded += len(expired)
                    self.record_event(
                        "AUTO_EXPIRE_SWEEP",
                        f"Refunded {len(expired)} expired deal(s): {', '.join(expired)}",
                    )

            sealed_height: int | None = None
            if self.auto_sequence_enabled and not self.engine.circuit_breaker_paused:
                block = self.engine.seal_pending_block()
                if block is not None:
                    self.blocks_auto_sealed += 1
                    sealed_height = block.height
                    self.record_event(
                        "BLOCK_SEALED",
                        f"Sequenced & sealed Block #{block.height} ({len(block.transactions)} tx, "
                        f"merkle {block.merkle_root[:12]}...)",
                    )

            inv = self.engine.check_invariants()
            self.last_invariants = inv
            if not inv["ok"] and not self.engine.circuit_breaker_paused:
                reason = "; ".join(inv["errors"])
                self.engine.set_circuit_breaker(True, reason=f"Watchdog invariant trip: {reason}")
                self.record_event(
                    "WATCHDOG_TRIP",
                    f"Automatic circuit breaker engaged due to invariant error: {reason}",
                    level="CRITICAL",
                )

            return {
                "tick": self.tick_count,
                "sealed_block_height": sealed_height,
                "invariants_ok": inv["ok"],
                "timestamp_ms": self.last_tick_ms,
            }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_single_tick()
            except Exception as exc:  # pragma: no cover - defensive daemon guard
                self.record_event("CONTROL_ERROR", f"Supervisor tick exception: {exc}", level="ERROR")
            self._wake_event.wait(timeout=self.tick_interval_sec)
            self._wake_event.clear()

    def status(self) -> dict[str, Any]:
        with self._lock:
            now_ms = int(time.time() * 1000)
            return {
                "daemon_alive": self.is_running,
                "auto_sequence_enabled": self.auto_sequence_enabled,
                "auto_expire_deals_enabled": self.auto_expire_deals_enabled,
                "tick_interval_sec": self.tick_interval_sec,
                "tick_count": self.tick_count,
                "blocks_auto_sealed": self.blocks_auto_sealed,
                "deals_auto_refunded": self.deals_auto_refunded,
                "uptime_sec": max(0.0, round((now_ms - self.started_at_ms) / 1000.0, 1)),
                "last_tick_ms": self.last_tick_ms,
                "circuit_breaker_paused": self.engine.circuit_breaker_paused,
                "circuit_breaker_reason": self.engine.circuit_breaker_reason,
                "invariants_ok": self.last_invariants.get("ok", True),
                "audit_log": list(self._audit_log),
            }
