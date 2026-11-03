# SPDX-License-Identifier: MIT
"""Ensure repository root is importable when running `pytest` directly."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[2])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
