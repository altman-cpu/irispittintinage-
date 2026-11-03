# SPDX-License-Identifier: MIT
"""Exact 18-decimal token arithmetic, address canonicalization, and hashing utilities."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

DECIMALS = 18
ONE_TST = 10**DECIMALS
MAX_SUPPLY_TST = 100_000_000
MAX_SUPPLY_WEI = MAX_SUPPLY_TST * ONE_TST
GNOSIS_CHAIN_ID = 100
CHIADO_TESTNET_CHAIN_ID = 10200

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalize_address(address: str) -> str:
    """Validate and canonicalize an EVM/Gnosis 20-byte hex address."""
    if not isinstance(address, str):
        raise ValueError("Address must be a string")
    cleaned = address.strip()
    if not _ADDRESS_RE.match(cleaned):
        raise ValueError(f"Invalid Gnosis/EVM address (expected 0x + 40 hex chars): {address!r}")
    return "0x" + cleaned[2:].lower()


def derive_address(label: str) -> str:
    """Deterministically derive a valid 20-byte 0x address from a human label."""
    digest = hashlib.sha256(f"tstreet-gnosis:{label}".encode("utf-8")).hexdigest()
    return "0x" + digest[:40]


def parse_tst_to_wei(amount: str | int | float | Decimal) -> int:
    """Convert a human TST amount (e.g. '1250.5') into exact integer wei (10**18)."""
    if isinstance(amount, bool):
        raise ValueError("Amount cannot be a boolean")
    try:
        dec = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid TST amount: {amount!r}") from exc

    if not dec.is_finite() or dec < 0:
        raise ValueError(f"Amount must be a finite non-negative number: {amount!r}")

    scaled = dec * Decimal(ONE_TST)
    integral = scaled.to_integral_value(rounding=ROUND_DOWN)
    if scaled != integral:
        raise ValueError(f"Amount exceeds {DECIMALS} decimal places: {amount!r}")
    return int(integral)


def format_wei_to_tst(amount_wei: int, max_fraction_digits: int = 6) -> str:
    """Format integer wei as a human-readable TST decimal string."""
    if amount_wei == 0:
        return "0"
    sign = "-" if amount_wei < 0 else ""
    abs_wei = abs(int(amount_wei))
    whole = abs_wei // ONE_TST
    frac = abs_wei % ONE_TST
    if frac == 0:
        return f"{sign}{whole:,}"
    frac_str = f"{frac:018d}".rstrip("0")
    if max_fraction_digits is not None and len(frac_str) > max_fraction_digits:
        frac_str = frac_str[:max_fraction_digits].rstrip("0")
    if not frac_str:
        return f"{sign}{whole:,}"
    return f"{sign}{whole:,}.{frac_str}"


def format_wei_exact(amount_wei: int) -> str:
    """Format integer wei into an ungrouped exact decimal string for APIs/forms."""
    if amount_wei == 0:
        return "0"
    sign = "-" if amount_wei < 0 else ""
    abs_wei = abs(int(amount_wei))
    whole = abs_wei // ONE_TST
    frac = abs_wei % ONE_TST
    if frac == 0:
        return f"{sign}{whole}"
    frac_str = f"{frac:018d}".rstrip("0")
    return f"{sign}{whole}.{frac_str}"


def canonical_json_bytes(data: Any) -> bytes:
    """Deterministic UTF-8 JSON serialization with sorted keys and compact separators."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    """Return 0x-prefixed SHA-256 hex digest."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return "0x" + hashlib.sha256(raw).hexdigest()
