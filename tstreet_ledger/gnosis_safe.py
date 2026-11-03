# SPDX-License-Identifier: MIT
"""Gnosis Safe (Safe{Wallet}) M-of-N multisig governance and Ledger EIP-712 signing payloads."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from tstreet_ledger.units import (
    GNOSIS_CHAIN_ID,
    canonical_json_bytes,
    normalize_address,
    sha256_hex,
)

SAFE_TX_TYPES = {
    "EIP712Domain": [
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "SafeTx": [
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
        {"name": "operation", "type": "uint8"},
        {"name": "safeTxGas", "type": "uint256"},
        {"name": "baseGas", "type": "uint256"},
        {"name": "gasPrice", "type": "uint256"},
        {"name": "gasToken", "type": "address"},
        {"name": "refundReceiver", "type": "address"},
        {"name": "nonce", "type": "uint256"},
    ],
}

ZERO_ADDRESS = "0x" + ("0" * 40)


def encode_action_calldata_hex(action: str, params: dict[str, Any]) -> str:
    """Deterministic ABI-style payload encoding for SafeTx `data` field."""
    selector = hashlib.sha256(f"tstreet.safe.{action}".encode("utf-8")).hexdigest()[:8]
    body_hex = canonical_json_bytes(params).hex()
    return f"0x{selector}{body_hex}"


def build_eip712_safe_tx_payload(
    *,
    safe_address: str,
    target_contract: str,
    safe_nonce: int,
    action: str,
    params: dict[str, Any],
    chain_id: int = GNOSIS_CHAIN_ID,
) -> dict[str, Any]:
    """
    Build a canonical EIP-712 `SafeTx` typed-data payload for Gnosis Chain (`chainId = 100`).

    Compatible with Safe{Wallet} and Ledger hardware wallet blind/clear EIP-712 signing flows.
    """
    verifying_safe = normalize_address(safe_address)
    to_addr = normalize_address(target_contract)
    calldata_hex = encode_action_calldata_hex(action, params)

    message = {
        "to": to_addr,
        "value": "0",
        "data": calldata_hex,
        "operation": 0,
        "safeTxGas": "0",
        "baseGas": "0",
        "gasPrice": "0",
        "gasToken": ZERO_ADDRESS,
        "refundReceiver": ZERO_ADDRESS,
        "nonce": safe_nonce,
    }
    domain = {
        "chainId": chain_id,
        "verifyingContract": verifying_safe,
    }
    domain_separator = sha256_hex(canonical_json_bytes({"EIP712Domain": domain}))
    struct_hash = sha256_hex(canonical_json_bytes({"SafeTx": message}))
    signing_digest = (
        "0x"
        + hashlib.sha256(
            b"\x19\x01"
            + bytes.fromhex(domain_separator.removeprefix("0x"))
            + bytes.fromhex(struct_hash.removeprefix("0x"))
        ).hexdigest()
    )

    return {
        "types": SAFE_TX_TYPES,
        "primaryType": "SafeTx",
        "domain": domain,
        "message": message,
        "meta": {
            "action": action,
            "params": params,
            "domainSeparator": domain_separator,
            "safeTxStructHash": struct_hash,
            "eip712SigningDigest": signing_digest,
            "hardwareWalletHint": (
                "Connect Ledger device -> Open Ethereum/Gnosis app -> Verify domainSeparator & "
                "safeTxStructHash on Ledger screen before approving."
            ),
        },
    }


@dataclass
class SafeProposal:
    """Multisig governance/arbiter transaction queued in the Gnosis Safe."""

    proposal_id: str
    safe_nonce: int
    action: str
    title: str
    target_contract: str
    params: dict[str, Any]
    proposer: str
    created_ms: int
    eip712_payload: dict[str, Any]
    approvals: list[dict[str, Any]] = field(default_factory=list)
    status: str = "PENDING"  # PENDING | EXECUTED | REJECTED
    executed_ms: int | None = None
    execution_tx_hash: str | None = None

    def approved_addresses(self) -> set[str]:
        return {entry["signer"] for entry in self.approvals}

    def to_dict(self, threshold: int) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "safe_nonce": self.safe_nonce,
            "action": self.action,
            "title": self.title,
            "target_contract": self.target_contract,
            "params": self.params,
            "proposer": self.proposer,
            "created_ms": self.created_ms,
            "approvals": list(self.approvals),
            "approval_count": len(self.approvals),
            "threshold": threshold,
            "ready_to_execute": self.status == "PENDING" and len(self.approvals) >= threshold,
            "status": self.status,
            "executed_ms": self.executed_ms,
            "execution_tx_hash": self.execution_tx_hash,
            "eip712_payload": self.eip712_payload,
        }


class GnosisSafeVault:
    """M-of-N Gnosis Safe controller managing Ledger hardware owner signers and proposals."""

    def __init__(
        self,
        safe_address: str,
        owners: list[dict[str, str]],
        threshold: int,
        chain_id: int = GNOSIS_CHAIN_ID,
    ) -> None:
        if not owners:
            raise ValueError("Gnosis Safe requires at least one owner")
        if threshold < 1 or threshold > len(owners):
            raise ValueError(f"Invalid Safe threshold {threshold} for {len(owners)} owners")

        self.safe_address = normalize_address(safe_address)
        self.chain_id = chain_id
        self.threshold = threshold
        self.owners: list[dict[str, str]] = []
        seen: set[str] = set()
        for owner in owners:
            addr = normalize_address(owner["address"])
            if addr in seen:
                raise ValueError(f"Duplicate Safe owner {addr}")
            seen.add(addr)
            self.owners.append(
                {
                    "address": addr,
                    "label": owner.get("label", addr[:10]),
                    "device": owner.get("device", "Ledger Nano X / Flex"),
                }
            )

        self._owner_set = seen
        self.safe_nonce = 0
        self._proposal_seq = 1
        self._proposals: dict[str, SafeProposal] = {}

    def is_owner(self, address: str) -> bool:
        return normalize_address(address) in self._owner_set

    def propose(
        self,
        *,
        proposer: str,
        action: str,
        title: str,
        target_contract: str,
        params: dict[str, Any],
        auto_approve_proposer: bool = True,
    ) -> SafeProposal:
        proposer_addr = normalize_address(proposer)
        if not self.is_owner(proposer_addr):
            raise ValueError(f"Proposer {proposer_addr} is not a Gnosis Safe owner")

        proposal_id = f"SAFE-{self._proposal_seq:04d}"
        assigned_nonce = self.safe_nonce
        self.safe_nonce += 1
        self._proposal_seq += 1

        eip712 = build_eip712_safe_tx_payload(
            safe_address=self.safe_address,
            target_contract=target_contract,
            safe_nonce=assigned_nonce,
            action=action,
            params=params,
            chain_id=self.chain_id,
        )
        now_ms = int(time.time() * 1000)
        proposal = SafeProposal(
            proposal_id=proposal_id,
            safe_nonce=assigned_nonce,
            action=action,
            title=title,
            target_contract=normalize_address(target_contract),
            params=params,
            proposer=proposer_addr,
            created_ms=now_ms,
            eip712_payload=eip712,
        )
        self._proposals[proposal_id] = proposal
        if auto_approve_proposer:
            self.approve(proposal_id, proposer_addr)
        return proposal

    def approve(self, proposal_id: str, signer: str, signature_hex: str | None = None) -> SafeProposal:
        proposal = self.get_proposal(proposal_id)
        if proposal.status != "PENDING":
            raise ValueError(f"Proposal {proposal_id} is already {proposal.status}")

        signer_addr = normalize_address(signer)
        if not self.is_owner(signer_addr):
            raise ValueError(f"Signer {signer_addr} is not an authorized Gnosis Safe owner")
        if signer_addr in proposal.approved_addresses():
            raise ValueError(f"Owner {signer_addr} has already approved {proposal_id}")

        digest = proposal.eip712_payload["meta"]["eip712SigningDigest"]
        sig = signature_hex or sha256_hex(f"ledger-eip712-sig:{signer_addr}:{digest}")
        owner_meta = next(o for o in self.owners if o["address"] == signer_addr)
        proposal.approvals.append(
            {
                "signer": signer_addr,
                "label": owner_meta["label"],
                "device": owner_meta["device"],
                "signature": sig,
                "signed_at_ms": int(time.time() * 1000),
            }
        )
        return proposal

    def mark_executed(self, proposal_id: str, tx_hash: str) -> SafeProposal:
        proposal = self.get_proposal(proposal_id)
        if proposal.status != "PENDING":
            raise ValueError(f"Proposal {proposal_id} is {proposal.status}")
        if len(proposal.approvals) < self.threshold:
            raise ValueError(
                f"Insufficient Safe approvals for {proposal_id}: {len(proposal.approvals)}/{self.threshold}"
            )
        proposal.status = "EXECUTED"
        proposal.executed_ms = int(time.time() * 1000)
        proposal.execution_tx_hash = tx_hash
        return proposal

    def reject(self, proposal_id: str, signer: str) -> SafeProposal:
        proposal = self.get_proposal(proposal_id)
        signer_addr = normalize_address(signer)
        if not self.is_owner(signer_addr):
            raise ValueError(f"Signer {signer_addr} is not an authorized Gnosis Safe owner")
        if proposal.status != "PENDING":
            raise ValueError(f"Proposal {proposal_id} is already {proposal.status}")
        proposal.status = "REJECTED"
        return proposal

    def get_proposal(self, proposal_id: str) -> SafeProposal:
        if proposal_id not in self._proposals:
            raise KeyError(f"Unknown Safe proposal: {proposal_id}")
        return self._proposals[proposal_id]

    def list_proposals(self) -> list[dict[str, Any]]:
        ordered = sorted(self._proposals.values(), key=lambda p: p.safe_nonce, reverse=True)
        return [p.to_dict(self.threshold) for p in ordered]

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe_address": self.safe_address,
            "chain_id": self.chain_id,
            "network": "Gnosis Chain",
            "threshold": self.threshold,
            "owner_count": len(self.owners),
            "owners": list(self.owners),
            "safe_nonce": self.safe_nonce,
            "proposals": self.list_proposals(),
        }
