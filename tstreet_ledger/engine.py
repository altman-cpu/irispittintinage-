# SPDX-License-Identifier: MIT
"""Core state machine for T-Street (TST) token, Gnosis Safe governance, and Deal escrow."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tstreet_ledger.gnosis_safe import GnosisSafeVault
from tstreet_ledger.ledger import LedgerBlock, LedgerTransaction, OrderedLedger
from tstreet_ledger.units import (
    GNOSIS_CHAIN_ID,
    MAX_SUPPLY_WEI,
    ONE_TST,
    canonical_json_bytes,
    derive_address,
    format_wei_exact,
    format_wei_to_tst,
    normalize_address,
    parse_tst_to_wei,
    sha256_hex,
)

MAX_TRANSFER_FEE_BPS = 500  # 5.00%
MAX_DEAL_FEE_BPS = 250  # 2.50%


@dataclass
class DealRecord:
    """Bilateral OTC / milestone deal backed by TST escrow and Gnosis Safe arbitration."""

    deal_id: str
    title: str
    buyer: str
    seller: str
    amount_wei: int
    escrow_locked_wei: int
    terms_hash: str
    terms_summary: str
    created_ms: int
    expires_ms: int
    state: str = "OPEN"  # OPEN | FUNDED | DELIVERED | RELEASED | DISPUTED | REFUNDED | CANCELLED
    dispute_reason: str = ""
    disputed_by: str = ""
    resolution_note: str = ""
    seller_payout_wei: int = 0
    buyer_refund_wei: int = 0
    protocol_fee_wei: int = 0
    updated_ms: int = 0

    def to_dict(self, labels: dict[str, str]) -> dict[str, Any]:
        return {
            "deal_id": self.deal_id,
            "title": self.title,
            "buyer": self.buyer,
            "buyer_label": labels.get(self.buyer, self.buyer[:10]),
            "seller": self.seller,
            "seller_label": labels.get(self.seller, self.seller[:10]),
            "amount_wei": str(self.amount_wei),
            "amount_tst": format_wei_to_tst(self.amount_wei),
            "amount_exact": format_wei_exact(self.amount_wei),
            "escrow_locked_wei": str(self.escrow_locked_wei),
            "escrow_locked_tst": format_wei_to_tst(self.escrow_locked_wei),
            "terms_hash": self.terms_hash,
            "terms_summary": self.terms_summary,
            "created_ms": self.created_ms,
            "expires_ms": self.expires_ms,
            "state": self.state,
            "dispute_reason": self.dispute_reason,
            "disputed_by": self.disputed_by,
            "resolution_note": self.resolution_note,
            "seller_payout_tst": format_wei_to_tst(self.seller_payout_wei),
            "buyer_refund_tst": format_wei_to_tst(self.buyer_refund_wei),
            "protocol_fee_tst": format_wei_to_tst(self.protocol_fee_wei),
            "updated_ms": self.updated_ms or self.created_ms,
        }


class TStreetProtocolEngine:
    """
    Thread-safe unified engine combining:
      - Ordered cryptographic ledger & priority/nonce mempool sequencer
      - T-Street (TST) 18-decimal ERC-20 token state machine (100M TST cap)
      - Gnosis Safe (Safe{Wallet}) M-of-N multisig governance & EIP-712 Ledger payloads
      - T-Street Gnosis & Deal escrow settlement & dispute resolution
    """

    def __init__(self, *, seed_demo_state: bool = True) -> None:
        self._lock = threading.RLock()
        self.ledger = OrderedLedger()

        # Protocol addresses on Gnosis Chain (chainId 100)
        self.token_contract = derive_address("contract:TStreetToken:v1")
        self.escrow_contract = derive_address("contract:TStreetDealEscrow:v1")
        self.safe_address = derive_address("gnosis-safe:t-street-governor-treasury")

        self.ledger_signer_1 = derive_address("owner:ledger-vault-1")
        self.ledger_signer_2 = derive_address("owner:ledger-vault-2")
        self.ledger_signer_3 = derive_address("owner:t-street-ops-3")

        self.desk_alpha = derive_address("desk:t-street-alpha")
        self.desk_gnosis = derive_address("desk:gnosis-settlement")
        self.market_maker = derive_address("desk:liquidity-reserve")

        self.account_labels: dict[str, str] = {
            self.safe_address: "T-Street Gnosis Safe Treasury",
            self.desk_alpha: "T-Street Alpha Desk",
            self.desk_gnosis: "Gnosis OTC Settlement Desk",
            self.market_maker: "T-Street Liquidity Reserve",
            self.ledger_signer_1: "Ledger Hardware Vault #1",
            self.ledger_signer_2: "Ledger Hardware Vault #2",
            self.ledger_signer_3: "T-Street Ops Hardware Signer #3",
        }

        self.safe = GnosisSafeVault(
            safe_address=self.safe_address,
            owners=[
                {
                    "address": self.ledger_signer_1,
                    "label": "Ledger Hardware Vault #1",
                    "device": "Ledger Flex (Gnosis App)",
                },
                {
                    "address": self.ledger_signer_2,
                    "label": "Ledger Hardware Vault #2",
                    "device": "Ledger Nano X (Gnosis App)",
                },
                {
                    "address": self.ledger_signer_3,
                    "label": "T-Street Ops Signer #3",
                    "device": "Ledger Stax (Gnosis App)",
                },
            ],
            threshold=2,
            chain_id=GNOSIS_CHAIN_ID,
        )

        self.token_name = "T-Street Gnosis Token"
        self.token_symbol = "TST"
        self.max_supply_wei = MAX_SUPPLY_WEI
        self.total_supply_wei = 0
        self.total_burned_wei = 0
        self.total_fees_collected_wei = 0
        self.transfer_fee_bps = 10  # 0.10%
        self.deal_fee_bps = 25  # 0.25%
        self.circuit_breaker_paused = False
        self.circuit_breaker_reason = ""

        self._balances: dict[str, int] = {addr: 0 for addr in self.account_labels}
        self._fee_exempt: set[str] = {self.safe_address, self.escrow_contract}
        self._deals: dict[str, DealRecord] = {}
        self._deal_seq = 1
        self._rejected_executions: list[dict[str, Any]] = []

        self._bootstrap_genesis(seed_demo_state=seed_demo_state)

    def _bootstrap_genesis(self, *, seed_demo_state: bool) -> None:
        genesis_allocations = [
            (self.safe_address, 12_000_000 * ONE_TST, "Gnosis Safe Protocol Treasury Reserve"),
            (self.market_maker, 5_000_000 * ONE_TST, "T-Street Liquidity & Market Making Pool"),
            (self.desk_alpha, 1_500_000 * ONE_TST, "T-Street Alpha Institutional Desk"),
            (self.desk_gnosis, 1_500_000 * ONE_TST, "Gnosis OTC Counterparty Desk"),
        ]
        genesis_txs: list[LedgerTransaction] = []
        for nonce_idx, (recipient, amount_wei, memo) in enumerate(genesis_allocations):
            self._balances[recipient] = self._balances.get(recipient, 0) + amount_wei
            self.total_supply_wei += amount_wei
            tx = LedgerTransaction(
                tx_type="GENESIS_MINT",
                sender=self.safe_address,
                recipient=recipient,
                amount_wei=amount_wei,
                priority_fee_wei=0,
                nonce=nonce_idx,
                created_ns=self.ledger.next_timestamp_ns(),
                payload={"memo": memo, "symbol": self.token_symbol, "chain_id": GNOSIS_CHAIN_ID},
            )
            genesis_txs.append(tx)

        self.ledger.seal_block(genesis_txs, state_root=self.compute_state_root())

        if seed_demo_state:
            now_ms = int(time.time() * 1000)
            # Sample Deal 1: Funded & Delivered OTC settlement ready for release
            d1 = self.create_deal(
                buyer=self.desk_alpha,
                seller=self.desk_gnosis,
                amount_tst="125000",
                title="Gnosis Validator Stake & xDAI Liquidity Tranche A",
                terms_summary="Deliver 125,000 TST OTC tranche against Gnosis Safe settlement receipt.",
                expires_in_seconds=86_400,
                seal_immediately=True,
            )
            self.fund_deal(deal_id=d1["deal_id"], caller=self.desk_alpha, seal_immediately=True)
            self.deliver_deal(deal_id=d1["deal_id"], caller=self.desk_gnosis, seal_immediately=True)

            # Sample Deal 2: Disputed deal escalated to the 2-of-3 Gnosis Safe arbiter
            d2 = self.create_deal(
                buyer=self.desk_gnosis,
                seller=self.market_maker,
                amount_tst="40000",
                title="Cross-Desk Market-Making Inventory Swap #02",
                terms_summary="Two-leg settlement scheduled before 16:00 UTC window.",
                expires_in_seconds=86_400,
                seal_immediately=True,
            )
            self.fund_deal(deal_id=d2["deal_id"], caller=self.desk_gnosis, seal_immediately=True)
            self.dispute_deal(
                deal_id=d2["deal_id"],
                caller=self.desk_gnosis,
                reason="Partial fill timing discrepancy; requesting 50/50 Safe split.",
                seal_immediately=True,
            )
            # Queue a live 1-of-2 approved Safe proposal for resolving Deal 2
            self.propose_safe_action(
                proposer=self.ledger_signer_1,
                action="RESOLVE_DEAL",
                title=f"Resolve {d2['deal_id']} (50% Seller / 50% Buyer Split)",
                params={"deal_id": d2["deal_id"], "seller_share_bps": 5000},
            )
            _ = now_ms

    def register_wallet(self, label: str, address: str | None = None) -> dict[str, Any]:
        """Register or label a wallet in the T-Street directory."""
        with self._lock:
            clean_label = label.strip()
            if not clean_label:
                raise ValueError("Wallet label cannot be empty")
            addr = (
                normalize_address(address) if address else derive_address(f"wallet:{clean_label}:{len(self._balances)}")
            )
            self.account_labels[addr] = clean_label
            self._balances.setdefault(addr, 0)
            return self.get_wallet(addr)

    def get_wallet(self, address: str) -> dict[str, Any]:
        with self._lock:
            addr = normalize_address(address)
            bal = self._balances.get(addr, 0)
            locked = sum(
                d.escrow_locked_wei for d in self._deals.values() if d.buyer == addr and d.escrow_locked_wei > 0
            )
            return {
                "address": addr,
                "label": self.account_labels.get(addr, addr[:10]),
                "balance_wei": str(bal),
                "balance_tst": format_wei_to_tst(bal),
                "balance_exact": format_wei_exact(bal),
                "escrow_locked_wei": str(locked),
                "escrow_locked_tst": format_wei_to_tst(locked),
                "confirmed_nonce": self.ledger.confirmed_nonce(addr),
                "next_nonce": self.ledger.next_available_nonce(addr),
                "fee_exempt": addr in self._fee_exempt,
                "is_safe_owner": self.safe.is_owner(addr),
            }

    def compute_state_root(self) -> str:
        """Compute deterministic cryptographic commitment over all token, nonce, and deal state."""
        with self._lock:
            snapshot = {
                "balances": {k: str(v) for k, v in sorted(self._balances.items()) if v > 0},
                "deals": {
                    k: {
                        "escrow_locked_wei": str(d.escrow_locked_wei),
                        "state": d.state,
                    }
                    for k, d in sorted(self._deals.items())
                },
                "deal_fee_bps": self.deal_fee_bps,
                "paused": self.circuit_breaker_paused,
                "total_burned_wei": str(self.total_burned_wei),
                "total_supply_wei": str(self.total_supply_wei),
                "transfer_fee_bps": self.transfer_fee_bps,
            }
            return sha256_hex(canonical_json_bytes(snapshot))

    def _require_not_paused(self) -> None:
        if self.circuit_breaker_paused:
            raise RuntimeError(
                f"Protocol circuit breaker is active ({self.circuit_breaker_reason or 'paused by operator'})"
            )

    def set_circuit_breaker(self, paused: bool, reason: str = "") -> dict[str, Any]:
        with self._lock:
            self.circuit_breaker_paused = bool(paused)
            self.circuit_breaker_reason = reason.strip() if paused else ""
            return {
                "paused": self.circuit_breaker_paused,
                "reason": self.circuit_breaker_reason,
            }

    # ------------------------------------------------------------------
    # Token & Mempool Transaction Submission
    # ------------------------------------------------------------------

    def submit_transfer(
        self,
        *,
        sender: str,
        recipient: str,
        amount_tst: str | int | float,
        priority_fee_tst: str | int | float = "0",
        memo: str = "",
        nonce: int | None = None,
        seal_immediately: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_not_paused()
            sender_addr = normalize_address(sender)
            recipient_addr = normalize_address(recipient)
            if sender_addr == recipient_addr:
                raise ValueError("Sender and recipient must be distinct addresses")

            amount_wei = parse_tst_to_wei(amount_tst)
            if amount_wei <= 0:
                raise ValueError("Transfer amount must be greater than 0 TST")
            prio_wei = parse_tst_to_wei(priority_fee_tst)

            avail = self._balances.get(sender_addr, 0)
            if avail < amount_wei + prio_wei:
                raise ValueError(
                    f"Insufficient TST balance for {sender_addr}: "
                    f"have {format_wei_to_tst(avail)} TST, need {format_wei_to_tst(amount_wei + prio_wei)} TST"
                )

            self._balances.setdefault(recipient_addr, 0)
            self.account_labels.setdefault(sender_addr, sender_addr[:10])
            self.account_labels.setdefault(recipient_addr, recipient_addr[:10])

            assigned_nonce = self.ledger.next_available_nonce(sender_addr) if nonce is None else int(nonce)
            tx = LedgerTransaction(
                tx_type="TRANSFER",
                sender=sender_addr,
                recipient=recipient_addr,
                amount_wei=amount_wei,
                priority_fee_wei=prio_wei,
                nonce=assigned_nonce,
                created_ns=self.ledger.next_timestamp_ns(),
                payload={"memo": memo.strip()},
            )
            self.ledger.enqueue(tx)
            block = self.seal_pending_block() if seal_immediately else None
            return {
                "tx": tx.to_dict(),
                "sealed_block": block.to_dict() if block else None,
            }

    def submit_burn(
        self,
        *,
        sender: str,
        amount_tst: str | int | float,
        priority_fee_tst: str | int | float = "0",
        reason: str = "",
        seal_immediately: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_not_paused()
            sender_addr = normalize_address(sender)
            amount_wei = parse_tst_to_wei(amount_tst)
            if amount_wei <= 0:
                raise ValueError("Burn amount must be greater than 0 TST")
            prio_wei = parse_tst_to_wei(priority_fee_tst)
            avail = self._balances.get(sender_addr, 0)
            if avail < amount_wei + prio_wei:
                raise ValueError("Insufficient TST balance for burn + priority fee")

            tx = LedgerTransaction(
                tx_type="BURN",
                sender=sender_addr,
                recipient=self.token_contract,
                amount_wei=amount_wei,
                priority_fee_wei=prio_wei,
                nonce=self.ledger.next_available_nonce(sender_addr),
                created_ns=self.ledger.next_timestamp_ns(),
                payload={"reason": reason.strip()},
            )
            self.ledger.enqueue(tx)
            block = self.seal_pending_block() if seal_immediately else None
            return {
                "tx": tx.to_dict(),
                "sealed_block": block.to_dict() if block else None,
            }

    # ------------------------------------------------------------------
    # T-Street Gnosis & Deal Lifecycle
    # ------------------------------------------------------------------

    def create_deal(
        self,
        *,
        buyer: str,
        seller: str,
        amount_tst: str | int | float,
        title: str,
        terms_summary: str = "",
        expires_in_seconds: int = 86_400,
        priority_fee_tst: str | int | float = "0",
        seal_immediately: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_not_paused()
            buyer_addr = normalize_address(buyer)
            seller_addr = normalize_address(seller)
            if buyer_addr == seller_addr:
                raise ValueError("Buyer and seller must be distinct addresses")
            clean_title = title.strip()
            if not clean_title:
                raise ValueError("Deal title is required")
            amount_wei = parse_tst_to_wei(amount_tst)
            if amount_wei <= 0:
                raise ValueError("Deal amount must be greater than 0 TST")
            if expires_in_seconds <= 0:
                raise ValueError("Deal expiry window must be positive")
            prio_wei = parse_tst_to_wei(priority_fee_tst)
            if self._balances.get(buyer_addr, 0) < prio_wei:
                raise ValueError("Insufficient buyer balance for priority fee")

            deal_id = f"DEAL-{self._deal_seq:04d}"
            self._deal_seq += 1
            now_ms = int(time.time() * 1000)
            expires_ms = now_ms + int(expires_in_seconds * 1000)
            terms_hash = sha256_hex(
                canonical_json_bytes(
                    {
                        "amount_wei": str(amount_wei),
                        "buyer": buyer_addr,
                        "deal_id": deal_id,
                        "expires_ms": expires_ms,
                        "seller": seller_addr,
                        "terms_summary": terms_summary.strip(),
                        "title": clean_title,
                    }
                )
            )

            self.account_labels.setdefault(buyer_addr, buyer_addr[:10])
            self.account_labels.setdefault(seller_addr, seller_addr[:10])
            self._balances.setdefault(buyer_addr, 0)
            self._balances.setdefault(seller_addr, 0)

            tx = LedgerTransaction(
                tx_type="DEAL_CREATE",
                sender=buyer_addr,
                recipient=seller_addr,
                amount_wei=amount_wei,
                priority_fee_wei=prio_wei,
                nonce=self.ledger.next_available_nonce(buyer_addr),
                created_ns=self.ledger.next_timestamp_ns(),
                payload={
                    "deal_id": deal_id,
                    "title": clean_title,
                    "terms_summary": terms_summary.strip(),
                    "terms_hash": terms_hash,
                    "created_ms": now_ms,
                    "expires_ms": expires_ms,
                },
            )
            self.ledger.enqueue(tx)
            if seal_immediately:
                self.seal_pending_block()
                return self._deals[deal_id].to_dict(self.account_labels)
            return {"deal_id": deal_id, "queued_tx": tx.to_dict()}

    def fund_deal(
        self,
        *,
        deal_id: str,
        caller: str,
        priority_fee_tst: str | int | float = "0",
        seal_immediately: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_not_paused()
            deal = self._get_deal(deal_id)
            caller_addr = normalize_address(caller)
            if caller_addr != deal.buyer:
                raise ValueError("Only the deal buyer can fund escrow")
            if deal.state != "OPEN":
                raise ValueError(f"Deal {deal_id} is {deal.state}, expected OPEN")
            prio_wei = parse_tst_to_wei(priority_fee_tst)
            avail = self._balances.get(caller_addr, 0)
            if avail < deal.amount_wei + prio_wei:
                raise ValueError(
                    f"Buyer {caller_addr} lacks TST to fund {deal_id}: "
                    f"have {format_wei_to_tst(avail)} TST, need {format_wei_to_tst(deal.amount_wei + prio_wei)} TST"
                )

            tx = LedgerTransaction(
                tx_type="DEAL_FUND",
                sender=caller_addr,
                recipient=self.escrow_contract,
                amount_wei=deal.amount_wei,
                priority_fee_wei=prio_wei,
                nonce=self.ledger.next_available_nonce(caller_addr),
                created_ns=self.ledger.next_timestamp_ns(),
                payload={"deal_id": deal_id},
            )
            self.ledger.enqueue(tx)
            if seal_immediately:
                self.seal_pending_block()
            return deal.to_dict(self.account_labels)

    def deliver_deal(
        self,
        *,
        deal_id: str,
        caller: str,
        delivery_note: str = "",
        seal_immediately: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_not_paused()
            deal = self._get_deal(deal_id)
            caller_addr = normalize_address(caller)
            if caller_addr != deal.seller:
                raise ValueError("Only the deal seller can mark delivery")
            if deal.state != "FUNDED":
                raise ValueError(f"Deal {deal_id} must be FUNDED before delivery (current: {deal.state})")

            tx = LedgerTransaction(
                tx_type="DEAL_DELIVER",
                sender=caller_addr,
                recipient=self.escrow_contract,
                amount_wei=0,
                priority_fee_wei=0,
                nonce=self.ledger.next_available_nonce(caller_addr),
                created_ns=self.ledger.next_timestamp_ns(),
                payload={"deal_id": deal_id, "delivery_note": delivery_note.strip()},
            )
            self.ledger.enqueue(tx)
            if seal_immediately:
                self.seal_pending_block()
            return deal.to_dict(self.account_labels)

    def release_deal(
        self,
        *,
        deal_id: str,
        caller: str,
        seal_immediately: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_not_paused()
            deal = self._get_deal(deal_id)
            caller_addr = normalize_address(caller)
            if caller_addr != deal.buyer:
                raise ValueError("Only the deal buyer can approve direct escrow release")
            if deal.state not in ("FUNDED", "DELIVERED"):
                raise ValueError(f"Deal {deal_id} cannot be released from state {deal.state}")

            tx = LedgerTransaction(
                tx_type="DEAL_RELEASE",
                sender=caller_addr,
                recipient=deal.seller,
                amount_wei=deal.escrow_locked_wei,
                priority_fee_wei=0,
                nonce=self.ledger.next_available_nonce(caller_addr),
                created_ns=self.ledger.next_timestamp_ns(),
                payload={"deal_id": deal_id},
            )
            self.ledger.enqueue(tx)
            if seal_immediately:
                self.seal_pending_block()
            return deal.to_dict(self.account_labels)

    def dispute_deal(
        self,
        *,
        deal_id: str,
        caller: str,
        reason: str,
        seal_immediately: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            deal = self._get_deal(deal_id)
            caller_addr = normalize_address(caller)
            if caller_addr not in (deal.buyer, deal.seller):
                raise ValueError("Only buyer or seller may raise a dispute on this deal")
            if deal.state not in ("FUNDED", "DELIVERED"):
                raise ValueError(f"Deal {deal_id} cannot be disputed in state {deal.state}")

            tx = LedgerTransaction(
                tx_type="DEAL_DISPUTE",
                sender=caller_addr,
                recipient=self.safe_address,
                amount_wei=0,
                priority_fee_wei=0,
                nonce=self.ledger.next_available_nonce(caller_addr),
                created_ns=self.ledger.next_timestamp_ns(),
                payload={"deal_id": deal_id, "reason": reason.strip() or "Escalated to Gnosis Safe"},
            )
            self.ledger.enqueue(tx)
            if seal_immediately:
                self.seal_pending_block()
            return deal.to_dict(self.account_labels)

    def sweep_expired_deals(self, now_ms: int | None = None) -> list[str]:
        """Auto-refund FUNDED deals past their expiry timestamp (used by continuous control plane)."""
        with self._lock:
            if self.circuit_breaker_paused:
                return []
            ts_ms = int(time.time() * 1000) if now_ms is None else now_ms
            refunded_ids: list[str] = []
            for deal in self._deals.values():
                if deal.state == "FUNDED" and ts_ms >= deal.expires_ms:
                    tx = LedgerTransaction(
                        tx_type="DEAL_EXPIRE_REFUND",
                        sender=self.safe_address,
                        recipient=deal.buyer,
                        amount_wei=deal.escrow_locked_wei,
                        priority_fee_wei=0,
                        nonce=self.ledger.next_available_nonce(self.safe_address),
                        created_ns=self.ledger.next_timestamp_ns(),
                        payload={"deal_id": deal.deal_id, "expired_at_ms": deal.expires_ms},
                    )
                    self.ledger.enqueue(tx)
                    refunded_ids.append(deal.deal_id)
            if refunded_ids:
                self.seal_pending_block()
            return refunded_ids

    # ------------------------------------------------------------------
    # Gnosis Safe Multisig Governance & Execution
    # ------------------------------------------------------------------

    def propose_safe_action(
        self,
        *,
        proposer: str,
        action: str,
        title: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a Gnosis Safe proposal with canonical EIP-712 payload for Ledger signers."""
        with self._lock:
            supported = {"SAFE_MINT", "RESOLVE_DEAL", "SET_FEES", "SET_CIRCUIT_BREAKER"}
            if action not in supported:
                raise ValueError(f"Unsupported Safe action {action!r}; must be one of {sorted(supported)}")

            target = self.token_contract
            normalized_params = dict(params)
            if action == "SAFE_MINT":
                to_addr = normalize_address(str(params["recipient"]))
                mint_wei = parse_tst_to_wei(params["amount_tst"])
                if mint_wei <= 0:
                    raise ValueError("Mint amount must be > 0 TST")
                if self.total_supply_wei + mint_wei > self.max_supply_wei:
                    raise ValueError("Mint would exceed 100,000,000 TST hard cap")
                normalized_params = {
                    "recipient": to_addr,
                    "amount_wei": str(mint_wei),
                    "amount_tst": format_wei_exact(mint_wei),
                }
            elif action == "RESOLVE_DEAL":
                target = self.escrow_contract
                deal = self._get_deal(str(params["deal_id"]))
                if deal.state != "DISPUTED":
                    raise ValueError(f"Deal {deal.deal_id} must be DISPUTED for Safe arbitration")
                share_bps = int(params.get("seller_share_bps", 10_000))
                if share_bps < 0 or share_bps > 10_000:
                    raise ValueError("seller_share_bps must be between 0 and 10000")
                normalized_params = {
                    "deal_id": deal.deal_id,
                    "seller_share_bps": share_bps,
                }
            elif action == "SET_FEES":
                tf_bps = int(params.get("transfer_fee_bps", self.transfer_fee_bps))
                df_bps = int(params.get("deal_fee_bps", self.deal_fee_bps))
                if tf_bps < 0 or tf_bps > MAX_TRANSFER_FEE_BPS:
                    raise ValueError(f"transfer_fee_bps must be 0..{MAX_TRANSFER_FEE_BPS}")
                if df_bps < 0 or df_bps > MAX_DEAL_FEE_BPS:
                    raise ValueError(f"deal_fee_bps must be 0..{MAX_DEAL_FEE_BPS}")
                normalized_params = {
                    "transfer_fee_bps": tf_bps,
                    "deal_fee_bps": df_bps,
                }
            elif action == "SET_CIRCUIT_BREAKER":
                normalized_params = {
                    "paused": bool(params.get("paused", False)),
                    "reason": str(params.get("reason", "Gnosis Safe governance directive")),
                }

            proposal = self.safe.propose(
                proposer=proposer,
                action=action,
                title=title.strip() or f"{action} Proposal",
                target_contract=target,
                params=normalized_params,
                auto_approve_proposer=True,
            )
            if len(proposal.approvals) >= self.safe.threshold:
                self._execute_safe_proposal(proposal.proposal_id)
            return proposal.to_dict(self.safe.threshold)

    def approve_safe_proposal(
        self,
        *,
        proposal_id: str,
        signer: str,
        signature_hex: str | None = None,
        auto_execute: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            proposal = self.safe.approve(proposal_id, signer, signature_hex=signature_hex)
            if auto_execute and len(proposal.approvals) >= self.safe.threshold:
                self._execute_safe_proposal(proposal_id)
            return proposal.to_dict(self.safe.threshold)

    def _execute_safe_proposal(self, proposal_id: str) -> None:
        proposal = self.safe.get_proposal(proposal_id)
        if proposal.action != "SET_CIRCUIT_BREAKER":
            self._require_not_paused()

        amount_wei = 0
        recipient = proposal.target_contract
        if proposal.action == "SAFE_MINT":
            amount_wei = int(proposal.params["amount_wei"])
            recipient = proposal.params["recipient"]

        tx = LedgerTransaction(
            tx_type=f"SAFE_{proposal.action}",
            sender=self.safe_address,
            recipient=recipient,
            amount_wei=amount_wei,
            priority_fee_wei=0,
            nonce=self.ledger.next_available_nonce(self.safe_address),
            created_ns=self.ledger.next_timestamp_ns(),
            payload={
                "proposal_id": proposal.proposal_id,
                "safe_nonce": proposal.safe_nonce,
                "approvals": [a["signer"] for a in proposal.approvals],
                "eip712_digest": proposal.eip712_payload["meta"]["eip712SigningDigest"],
                "params": proposal.params,
            },
        )
        self.ledger.enqueue(tx)
        self.seal_pending_block()
        self.safe.mark_executed(proposal_id, tx.tx_hash)

    # ------------------------------------------------------------------
    # Deterministic Block Sequencer Execution
    # ------------------------------------------------------------------

    def seal_pending_block(
        self,
        *,
        max_transactions: int = 250,
        allow_empty: bool = False,
    ) -> LedgerBlock | None:
        """
        Sequence ready mempool transactions in deterministic order, apply state transitions,
        compute the updated cryptographic state root, and seal the next block.
        """
        with self._lock:
            ordered_candidates = self.ledger.preview_ordered_mempool(max_count=max_transactions)
            if not ordered_candidates and not allow_empty:
                return None

            executed: list[LedgerTransaction] = []
            for tx in ordered_candidates:
                try:
                    self._apply_transaction_state(tx)
                    executed.append(tx)
                except Exception as exc:
                    self.ledger.drop_mempool_tx(tx.tx_hash)
                    self._rejected_executions.append(
                        {
                            "tx_hash": tx.tx_hash,
                            "tx_type": tx.tx_type,
                            "error": str(exc),
                            "timestamp_ms": int(time.time() * 1000),
                        }
                    )

            if not executed and not allow_empty:
                return None

            state_root = self.compute_state_root()
            return self.ledger.seal_block(executed, state_root=state_root)

    def _charge_priority_fee(self, payer: str, priority_fee_wei: int) -> None:
        if priority_fee_wei <= 0:
            return
        bal = self._balances.get(payer, 0)
        if bal < priority_fee_wei:
            raise ValueError(f"Insufficient balance for priority fee by {payer}")
        self._balances[payer] = bal - priority_fee_wei
        self._balances[self.safe_address] = self._balances.get(self.safe_address, 0) + priority_fee_wei
        self.total_fees_collected_wei += priority_fee_wei

    def _apply_transaction_state(self, tx: LedgerTransaction) -> None:
        t = tx.tx_type
        now_ms = int(time.time() * 1000)

        if t == "TRANSFER":
            total_needed = tx.amount_wei + tx.priority_fee_wei
            sender_bal = self._balances.get(tx.sender, 0)
            if sender_bal < total_needed:
                raise ValueError("Insufficient TST balance at execution")
            self._charge_priority_fee(tx.sender, tx.priority_fee_wei)
            self._balances[tx.sender] -= tx.amount_wei

            fee_wei = 0
            if self.transfer_fee_bps > 0 and tx.sender not in self._fee_exempt and tx.recipient not in self._fee_exempt:
                fee_wei = (tx.amount_wei * self.transfer_fee_bps) // 10_000
            net_wei = tx.amount_wei - fee_wei
            self._balances[tx.recipient] = self._balances.get(tx.recipient, 0) + net_wei
            if fee_wei > 0:
                self._balances[self.safe_address] += fee_wei
                self.total_fees_collected_wei += fee_wei

        elif t == "BURN":
            total_needed = tx.amount_wei + tx.priority_fee_wei
            if self._balances.get(tx.sender, 0) < total_needed:
                raise ValueError("Insufficient balance for burn")
            self._charge_priority_fee(tx.sender, tx.priority_fee_wei)
            self._balances[tx.sender] -= tx.amount_wei
            self.total_supply_wei -= tx.amount_wei
            self.total_burned_wei += tx.amount_wei

        elif t == "DEAL_CREATE":
            self._charge_priority_fee(tx.sender, tx.priority_fee_wei)
            p = tx.payload
            self._deals[p["deal_id"]] = DealRecord(
                deal_id=p["deal_id"],
                title=p["title"],
                buyer=tx.sender,
                seller=tx.recipient,
                amount_wei=tx.amount_wei,
                escrow_locked_wei=0,
                terms_hash=p["terms_hash"],
                terms_summary=p["terms_summary"],
                created_ms=p["created_ms"],
                expires_ms=p["expires_ms"],
                state="OPEN",
                updated_ms=now_ms,
            )

        elif t == "DEAL_FUND":
            deal = self._get_deal(tx.payload["deal_id"])
            if deal.state != "OPEN":
                raise ValueError(f"Deal {deal.deal_id} not OPEN")
            total_needed = deal.amount_wei + tx.priority_fee_wei
            if self._balances.get(tx.sender, 0) < total_needed:
                raise ValueError("Insufficient buyer balance to lock escrow")
            self._charge_priority_fee(tx.sender, tx.priority_fee_wei)
            self._balances[tx.sender] -= deal.amount_wei
            deal.escrow_locked_wei = deal.amount_wei
            deal.state = "FUNDED"
            deal.updated_ms = now_ms

        elif t == "DEAL_DELIVER":
            deal = self._get_deal(tx.payload["deal_id"])
            if deal.state != "FUNDED":
                raise ValueError(f"Deal {deal.deal_id} not FUNDED")
            deal.state = "DELIVERED"
            deal.updated_ms = now_ms

        elif t == "DEAL_RELEASE":
            deal = self._get_deal(tx.payload["deal_id"])
            if deal.state not in ("FUNDED", "DELIVERED"):
                raise ValueError(f"Deal {deal.deal_id} cannot release from {deal.state}")
            locked = deal.escrow_locked_wei
            fee_wei = (locked * self.deal_fee_bps) // 10_000
            seller_net = locked - fee_wei
            deal.escrow_locked_wei = 0
            deal.seller_payout_wei = seller_net
            deal.protocol_fee_wei = fee_wei
            deal.state = "RELEASED"
            deal.updated_ms = now_ms

            self._balances[deal.seller] = self._balances.get(deal.seller, 0) + seller_net
            if fee_wei > 0:
                self._balances[self.safe_address] += fee_wei
                self.total_fees_collected_wei += fee_wei

        elif t == "DEAL_DISPUTE":
            deal = self._get_deal(tx.payload["deal_id"])
            if deal.state not in ("FUNDED", "DELIVERED"):
                raise ValueError(f"Deal {deal.deal_id} cannot dispute from {deal.state}")
            deal.state = "DISPUTED"
            deal.disputed_by = tx.sender
            deal.dispute_reason = tx.payload.get("reason", "")
            deal.updated_ms = now_ms

        elif t == "DEAL_EXPIRE_REFUND":
            deal = self._get_deal(tx.payload["deal_id"])
            if deal.state != "FUNDED":
                raise ValueError(f"Deal {deal.deal_id} not FUNDED")
            locked = deal.escrow_locked_wei
            deal.escrow_locked_wei = 0
            deal.buyer_refund_wei = locked
            deal.state = "REFUNDED"
            deal.resolution_note = "Expired deal automatically refunded by Control Plane"
            deal.updated_ms = now_ms
            self._balances[deal.buyer] = self._balances.get(deal.buyer, 0) + locked

        elif t == "SAFE_SAFE_MINT":
            params = tx.payload["params"]
            mint_wei = int(params["amount_wei"])
            to_addr = normalize_address(params["recipient"])
            if self.total_supply_wei + mint_wei > self.max_supply_wei:
                raise ValueError("Exceeds max TST supply")
            self.total_supply_wei += mint_wei
            self._balances[to_addr] = self._balances.get(to_addr, 0) + mint_wei
            self.account_labels.setdefault(to_addr, to_addr[:10])

        elif t == "SAFE_RESOLVE_DEAL":
            params = tx.payload["params"]
            deal = self._get_deal(params["deal_id"])
            if deal.state != "DISPUTED":
                raise ValueError(f"Deal {deal.deal_id} is {deal.state}, expected DISPUTED")
            share_bps = int(params["seller_share_bps"])
            locked = deal.escrow_locked_wei
            gross_seller = (locked * share_bps) // 10_000
            buyer_refund = locked - gross_seller
            fee_wei = (gross_seller * self.deal_fee_bps) // 10_000
            seller_net = gross_seller - fee_wei

            deal.escrow_locked_wei = 0
            deal.seller_payout_wei = seller_net
            deal.buyer_refund_wei = buyer_refund
            deal.protocol_fee_wei = fee_wei
            deal.state = "RELEASED" if share_bps > 0 else "REFUNDED"
            deal.resolution_note = (
                f"Resolved by Gnosis Safe ({tx.payload['proposal_id']}): "
                f"{share_bps / 100:.2f}% seller / {(10_000 - share_bps) / 100:.2f}% buyer"
            )
            deal.updated_ms = now_ms

            if seller_net > 0:
                self._balances[deal.seller] = self._balances.get(deal.seller, 0) + seller_net
            if buyer_refund > 0:
                self._balances[deal.buyer] = self._balances.get(deal.buyer, 0) + buyer_refund
            if fee_wei > 0:
                self._balances[self.safe_address] += fee_wei
                self.total_fees_collected_wei += fee_wei

        elif t == "SAFE_SET_FEES":
            params = tx.payload["params"]
            self.transfer_fee_bps = int(params["transfer_fee_bps"])
            self.deal_fee_bps = int(params["deal_fee_bps"])

        elif t == "SAFE_SET_CIRCUIT_BREAKER":
            params = tx.payload["params"]
            self.circuit_breaker_paused = bool(params["paused"])
            self.circuit_breaker_reason = str(params.get("reason", "")) if self.circuit_breaker_paused else ""

        else:
            raise ValueError(f"Unknown transaction type: {t}")

    def _get_deal(self, deal_id: str) -> DealRecord:
        if deal_id not in self._deals:
            raise KeyError(f"Unknown deal ID: {deal_id}")
        return self._deals[deal_id]

    # ------------------------------------------------------------------
    # Continuous Invariant Verification & Snapshots
    # ------------------------------------------------------------------

    def check_invariants(self) -> dict[str, Any]:
        """Run full cryptographic & accounting conservation checks across ledger, token, and deals."""
        with self._lock:
            errors: list[str] = []
            chain_ok, chain_errors = self.ledger.verify_chain()
            if not chain_ok:
                errors.extend(chain_errors)

            negative_accounts = [addr for addr, bal in self._balances.items() if bal < 0]
            if negative_accounts:
                errors.append(f"Negative balances detected: {negative_accounts}")

            wallet_sum = sum(self._balances.values())
            escrow_sum = sum(d.escrow_locked_wei for d in self._deals.values())
            accounted_total = wallet_sum + escrow_sum

            if accounted_total != self.total_supply_wei:
                errors.append(
                    f"Supply conservation violation: wallets({wallet_sum}) + escrows({escrow_sum}) "
                    f"!= total_supply({self.total_supply_wei})"
                )

            if self.total_supply_wei > self.max_supply_wei:
                errors.append(f"Total supply {self.total_supply_wei} exceeds hard cap {self.max_supply_wei}")

            for p in self.safe.list_proposals():
                if p["status"] == "EXECUTED" and p["approval_count"] < self.safe.threshold:
                    errors.append(f"Executed Safe proposal {p['proposal_id']} below threshold")

            return {
                "ok": len(errors) == 0,
                "chain_verified": chain_ok,
                "supply_conserved": accounted_total == self.total_supply_wei,
                "wallet_balance_sum_tst": format_wei_to_tst(wallet_sum),
                "escrow_locked_sum_tst": format_wei_to_tst(escrow_sum),
                "total_supply_tst": format_wei_to_tst(self.total_supply_wei),
                "state_root": self.compute_state_root(),
                "errors": errors,
                "checked_at_ms": int(time.time() * 1000),
            }

    def load_contracts_source(self) -> dict[str, str]:
        contracts_dir = Path(__file__).resolve().parent / "contracts"
        result: dict[str, str] = {}
        for name in ("TStreetToken.sol", "TStreetDealEscrow.sol"):
            path = contracts_dir / name
            if path.exists():
                result[name] = path.read_text(encoding="utf-8")
        return result

    def snapshot(self) -> dict[str, Any]:
        """Serialize complete system state for the Live Control Center dashboard."""
        with self._lock:
            wallets = [self.get_wallet(addr) for addr in self.account_labels]
            wallets.sort(key=lambda w: int(w["balance_wei"]), reverse=True)

            deals = [
                d.to_dict(self.account_labels)
                for d in sorted(self._deals.values(), key=lambda item: item.deal_id, reverse=True)
            ]
            blocks = [b.to_dict() for b in reversed(self.ledger.blocks[-25:])]
            ordered_mempool = [tx.to_dict() for tx in self.ledger.preview_ordered_mempool()]
            unready_mempool = [tx.to_dict() for tx in self.ledger.unready_mempool_transactions()]
            invariants = self.check_invariants()

            return {
                "token": {
                    "name": self.token_name,
                    "symbol": self.token_symbol,
                    "decimals": 18,
                    "chain_id": GNOSIS_CHAIN_ID,
                    "network": "Gnosis Chain (xDAI / Safe{Wallet} Ready)",
                    "token_contract": self.token_contract,
                    "escrow_contract": self.escrow_contract,
                    "total_supply_wei": str(self.total_supply_wei),
                    "total_supply_tst": format_wei_to_tst(self.total_supply_wei),
                    "max_supply_tst": format_wei_to_tst(self.max_supply_wei),
                    "total_burned_tst": format_wei_to_tst(self.total_burned_wei),
                    "total_fees_collected_tst": format_wei_to_tst(self.total_fees_collected_wei),
                    "transfer_fee_bps": self.transfer_fee_bps,
                    "deal_fee_bps": self.deal_fee_bps,
                    "circuit_breaker_paused": self.circuit_breaker_paused,
                    "circuit_breaker_reason": self.circuit_breaker_reason,
                },
                "ledger": {
                    "height": self.ledger.height,
                    "sequencer_epoch": self.ledger.sequencer_epoch,
                    "latest_block_hash": self.ledger.blocks[-1].block_hash if self.ledger.blocks else "",
                    "latest_state_root": self.compute_state_root(),
                    "mempool_ready_count": len(ordered_mempool),
                    "mempool_unready_count": len(unready_mempool),
                    "ordered_mempool": ordered_mempool,
                    "unready_mempool": unready_mempool,
                    "blocks": blocks,
                    "rejected_executions": list(reversed(self._rejected_executions[-10:])),
                },
                "wallets": wallets,
                "deals": deals,
                "safe": self.safe.to_dict(),
                "invariants": invariants,
            }
