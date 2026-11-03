# SPDX-License-Identifier: MIT
"""Deterministic transaction sequencer and Merkle-authenticated append-only ledger."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from tstreet_ledger.units import canonical_json_bytes, format_wei_to_tst, normalize_address, sha256_hex

GENESIS_PREV_HASH = "0x" + ("0" * 64)
EMPTY_MERKLE_ROOT = sha256_hex(b"TSTREET_EMPTY_BLOCK")


def _pair_hash(left_hex: str, right_hex: str) -> str:
    left_bytes = bytes.fromhex(left_hex.removeprefix("0x"))
    right_bytes = bytes.fromhex(right_hex.removeprefix("0x"))
    return "0x" + hashlib.sha256(b"\x01" + left_bytes + right_bytes).hexdigest()


def _leaf_hash(tx_hash_hex: str) -> str:
    tx_bytes = bytes.fromhex(tx_hash_hex.removeprefix("0x"))
    return "0x" + hashlib.sha256(b"\x00" + tx_bytes).hexdigest()


def compute_merkle_root(tx_hashes: list[str]) -> str:
    """Compute a binary domain-separated SHA-256 Merkle root over ordered transaction hashes."""
    if not tx_hashes:
        return EMPTY_MERKLE_ROOT

    level = [_leaf_hash(h) for h in tx_hashes]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        next_level: list[str] = []
        for idx in range(0, len(level), 2):
            next_level.append(_pair_hash(level[idx], level[idx + 1]))
        level = next_level
    return level[0]


def build_merkle_proof(tx_hashes: list[str], index: int) -> list[dict[str, str]]:
    """Construct a verifiable Merkle inclusion proof for `tx_hashes[index]`."""
    if index < 0 or index >= len(tx_hashes):
        raise IndexError(f"Transaction index {index} out of bounds for {len(tx_hashes)} transactions")

    proof: list[dict[str, str]] = []
    level = [_leaf_hash(h) for h in tx_hashes]
    pos = index

    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        is_right = pos % 2 == 1
        sibling_idx = pos - 1 if is_right else pos + 1
        proof.append(
            {
                "position": "left" if is_right else "right",
                "hash": level[sibling_idx],
            }
        )
        next_level: list[str] = []
        for idx in range(0, len(level), 2):
            next_level.append(_pair_hash(level[idx], level[idx + 1]))
        level = next_level
        pos //= 2

    return proof


def verify_merkle_proof(tx_hash: str, proof: list[dict[str, str]], expected_root: str) -> bool:
    """Verify a Merkle inclusion proof against `expected_root`."""
    current = _leaf_hash(tx_hash)
    for step in proof:
        sibling = step["hash"]
        if step["position"] == "left":
            current = _pair_hash(sibling, current)
        elif step["position"] == "right":
            current = _pair_hash(current, sibling)
        else:
            return False
    return current == expected_root


@dataclass(frozen=True)
class LedgerTransaction:
    """Immutable cryptographically hashed ledger transaction."""

    tx_type: str
    sender: str
    recipient: str
    amount_wei: int
    priority_fee_wei: int
    nonce: int
    created_ns: int
    payload: dict[str, Any] = field(default_factory=dict)
    tx_hash: str = ""

    def __post_init__(self) -> None:
        canonical_sender = normalize_address(self.sender)
        canonical_recipient = normalize_address(self.recipient)
        object.__setattr__(self, "sender", canonical_sender)
        object.__setattr__(self, "recipient", canonical_recipient)
        if not self.tx_hash:
            object.__setattr__(self, "tx_hash", self.compute_hash())

    def compute_hash(self) -> str:
        body = {
            "amount_wei": str(self.amount_wei),
            "created_ns": self.created_ns,
            "nonce": self.nonce,
            "payload": self.payload,
            "priority_fee_wei": str(self.priority_fee_wei),
            "recipient": self.recipient,
            "sender": self.sender,
            "tx_type": self.tx_type,
        }
        return sha256_hex(canonical_json_bytes(body))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_hash": self.tx_hash,
            "tx_type": self.tx_type,
            "sender": self.sender,
            "recipient": self.recipient,
            "amount_wei": str(self.amount_wei),
            "amount_tst": format_wei_to_tst(self.amount_wei),
            "priority_fee_wei": str(self.priority_fee_wei),
            "priority_fee_tst": format_wei_to_tst(self.priority_fee_wei),
            "nonce": self.nonce,
            "created_ns": self.created_ns,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class LedgerBlock:
    """Sealed, ordered, hash-chained ledger block with Merkle & state commitments."""

    height: int
    prev_hash: str
    timestamp_ms: int
    sequencer_epoch: int
    state_root: str
    transactions: tuple[LedgerTransaction, ...]
    merkle_root: str = ""
    block_hash: str = ""

    def __post_init__(self) -> None:
        computed_merkle = compute_merkle_root([tx.tx_hash for tx in self.transactions])
        if not self.merkle_root:
            object.__setattr__(self, "merkle_root", computed_merkle)
        if not self.block_hash:
            object.__setattr__(self, "block_hash", self.compute_hash())

    def compute_hash(self) -> str:
        header = {
            "height": self.height,
            "merkle_root": self.merkle_root,
            "prev_hash": self.prev_hash,
            "sequencer_epoch": self.sequencer_epoch,
            "state_root": self.state_root,
            "timestamp_ms": self.timestamp_ms,
            "tx_count": len(self.transactions),
        }
        return sha256_hex(canonical_json_bytes(header))

    def to_dict(self) -> dict[str, Any]:
        return {
            "height": self.height,
            "block_hash": self.block_hash,
            "prev_hash": self.prev_hash,
            "merkle_root": self.merkle_root,
            "state_root": self.state_root,
            "timestamp_ms": self.timestamp_ms,
            "sequencer_epoch": self.sequencer_epoch,
            "tx_count": len(self.transactions),
            "transactions": [tx.to_dict() for tx in self.transactions],
        }


class OrderedLedger:
    """
    Deterministic transaction sequencer and hash-chained block ledger.

    Ordering rule across ready transactions:
      1. Per-sender causal nonce order (nonce == confirmed_nonce[sender], then +1, ...).
      2. Highest `priority_fee_wei` first across distinct ready heads.
      3. Earliest monotonic `created_ns` arrival order.
      4. Canonical lexicographic `tx_hash` tie-breaker.
    """

    def __init__(self) -> None:
        self._blocks: list[LedgerBlock] = []
        self._mempool: dict[str, LedgerTransaction] = {}
        self._tx_index: dict[str, tuple[int, int]] = {}
        self._confirmed_nonces: dict[str, int] = {}
        self._monotonic_ns: int = time.time_ns()
        self.sequencer_epoch: int = 1

    def next_timestamp_ns(self) -> int:
        now = time.time_ns()
        if now <= self._monotonic_ns:
            self._monotonic_ns += 1
        else:
            self._monotonic_ns = now
        return self._monotonic_ns

    @property
    def height(self) -> int:
        return len(self._blocks) - 1

    @property
    def blocks(self) -> list[LedgerBlock]:
        return list(self._blocks)

    @property
    def mempool_size(self) -> int:
        return len(self._mempool)

    def confirmed_nonce(self, address: str) -> int:
        addr = normalize_address(address)
        return self._confirmed_nonces.get(addr, 0)

    def next_available_nonce(self, address: str) -> int:
        """Return next sequential nonce accounting for both confirmed and queued mempool txs."""
        addr = normalize_address(address)
        expected = self.confirmed_nonce(addr)
        sender_nonces = sorted(tx.nonce for tx in self._mempool.values() if tx.sender == addr)
        for queued_nonce in sender_nonces:
            if queued_nonce == expected:
                expected += 1
            elif queued_nonce > expected:
                break
        return expected

    def enqueue(self, tx: LedgerTransaction) -> LedgerTransaction:
        """Validate and admit a transaction into the ordered mempool."""
        if tx.tx_hash in self._mempool or tx.tx_hash in self._tx_index:
            raise ValueError(f"Duplicate transaction replay rejected: {tx.tx_hash}")

        confirmed = self.confirmed_nonce(tx.sender)
        if tx.nonce < confirmed:
            raise ValueError(f"Stale nonce {tx.nonce} for {tx.sender} (confirmed nonce is already {confirmed})")

        for existing in self._mempool.values():
            if existing.sender == tx.sender and existing.nonce == tx.nonce:
                raise ValueError(
                    f"Conflicting mempool nonce {tx.nonce} for sender {tx.sender} (existing tx {existing.tx_hash})"
                )

        self._mempool[tx.tx_hash] = tx
        return tx

    def preview_ordered_mempool(self, max_count: int | None = None) -> list[LedgerTransaction]:
        """
        Return the exact deterministic execution sequence of ready mempool transactions.

        Uses a priority frontier over each sender's next sequential nonce so causal
        per-sender ordering is never violated while higher-priority-fee transactions
        from competing accounts are sequenced ahead.
        """
        by_sender: dict[str, dict[int, LedgerTransaction]] = {}
        for tx in self._mempool.values():
            by_sender.setdefault(tx.sender, {})[tx.nonce] = tx

        cursors: dict[str, int] = {sender: self.confirmed_nonce(sender) for sender in by_sender}
        ordered: list[LedgerTransaction] = []

        while True:
            if max_count is not None and len(ordered) >= max_count:
                break
            candidates: list[LedgerTransaction] = []
            for sender, nonce_map in by_sender.items():
                nxt = cursors[sender]
                if nxt in nonce_map:
                    candidates.append(nonce_map[nxt])
            if not candidates:
                break
            candidates.sort(key=lambda t: (-t.priority_fee_wei, t.created_ns, t.tx_hash))
            winner = candidates[0]
            ordered.append(winner)
            cursors[winner.sender] += 1

        return ordered

    def unready_mempool_transactions(self) -> list[LedgerTransaction]:
        """Transactions waiting on a nonce gap from their sender."""
        ready_hashes = {tx.tx_hash for tx in self.preview_ordered_mempool()}
        unready = [tx for tx in self._mempool.values() if tx.tx_hash not in ready_hashes]
        unready.sort(key=lambda t: (t.sender, t.nonce, t.created_ns))
        return unready

    def seal_block(
        self,
        transactions: list[LedgerTransaction],
        state_root: str,
        timestamp_ms: int | None = None,
    ) -> LedgerBlock:
        """Seal an ordered list of executed transactions into the next immutable block."""
        prev_hash = GENESIS_PREV_HASH if not self._blocks else self._blocks[-1].block_hash
        next_height = len(self._blocks)
        ts_ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms

        for tx in transactions:
            expected = self.confirmed_nonce(tx.sender)
            if tx.nonce != expected:
                raise ValueError(f"Cannot seal out-of-order nonce {tx.nonce} for {tx.sender} (expected {expected})")
            self._confirmed_nonces[tx.sender] = expected + 1
            self._mempool.pop(tx.tx_hash, None)

        block = LedgerBlock(
            height=next_height,
            prev_hash=prev_hash,
            timestamp_ms=ts_ms,
            sequencer_epoch=self.sequencer_epoch,
            state_root=state_root,
            transactions=tuple(transactions),
        )
        self._blocks.append(block)
        for idx, tx in enumerate(transactions):
            self._tx_index[tx.tx_hash] = (next_height, idx)
        self.sequencer_epoch += 1
        return block

    def drop_mempool_tx(self, tx_hash: str) -> None:
        self._mempool.pop(tx_hash, None)

    def lookup_transaction(self, tx_hash: str) -> dict[str, Any] | None:
        """Locate a confirmed or pending transaction and build its Merkle proof if sealed."""
        if tx_hash in self._mempool:
            return {
                "status": "PENDING_MEMPOOL",
                "transaction": self._mempool[tx_hash].to_dict(),
                "block_height": None,
                "merkle_proof": None,
            }
        loc = self._tx_index.get(tx_hash)
        if loc is None:
            return None
        block_height, tx_idx = loc
        block = self._blocks[block_height]
        tx_hashes = [t.tx_hash for t in block.transactions]
        proof = build_merkle_proof(tx_hashes, tx_idx)
        verified = verify_merkle_proof(tx_hash, proof, block.merkle_root)
        return {
            "status": "CONFIRMED",
            "block_height": block.height,
            "block_hash": block.block_hash,
            "merkle_root": block.merkle_root,
            "state_root": block.state_root,
            "tx_index": tx_idx,
            "transaction": block.transactions[tx_idx].to_dict(),
            "merkle_proof": proof,
            "merkle_verified": verified,
        }

    def verify_chain(self) -> tuple[bool, list[str]]:
        """Cryptographically verify the entire chain: hashes, links, Merkle roots, and nonces."""
        errors: list[str] = []
        replay_nonces: dict[str, int] = {}
        seen_hashes: set[str] = set()

        for expected_height, block in enumerate(self._blocks):
            if block.height != expected_height:
                errors.append(f"Block height mismatch: expected {expected_height}, got {block.height}")

            expected_prev = GENESIS_PREV_HASH if expected_height == 0 else self._blocks[expected_height - 1].block_hash
            if block.prev_hash != expected_prev:
                errors.append(f"Broken prev_hash link at height {block.height}")

            recomputed_merkle = compute_merkle_root([tx.tx_hash for tx in block.transactions])
            if block.merkle_root != recomputed_merkle:
                errors.append(f"Merkle root mismatch at height {block.height}")

            if block.block_hash != block.compute_hash():
                errors.append(f"Block hash mismatch at height {block.height}")

            for idx, tx in enumerate(block.transactions):
                if tx.tx_hash != tx.compute_hash():
                    errors.append(f"Tampered tx hash in block {block.height} index {idx}")
                if tx.tx_hash in seen_hashes:
                    errors.append(f"Duplicate tx hash {tx.tx_hash} at block {block.height}")
                seen_hashes.add(tx.tx_hash)

                exp_nonce = replay_nonces.get(tx.sender, 0)
                if tx.nonce != exp_nonce:
                    errors.append(
                        f"Nonce order violation in block {block.height} for {tx.sender}: "
                        f"got {tx.nonce}, expected {exp_nonce}"
                    )
                replay_nonces[tx.sender] = exp_nonce + 1

        return (len(errors) == 0, errors)
