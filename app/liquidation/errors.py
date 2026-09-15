"""Decode Twyne custom errors by name (DEV-661).

A reverted ``eth_call`` or gas estimate reaches the bot as a web3 exception that
carries the raw revert data. A custom error therefore shows in the log as an opaque
4-byte selector. This module builds a selector-to-signature map from the vendored
Twyne ABI files, so the log shows `EnforcedPause()` or `SubAccountBlocked()` and an
operator sees immediately that the factory is paused or that the borrower is an EVC
sub-account.

The map is built once at import from ``contracts/*.json``. It is a diagnostic aid
only: a missing or unreadable ABI file leaves the map empty and every caller falls
back to the plain exception text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Optional

from eth_utils import keccak

# Vendored ABI files that hold the custom errors of the Twyne contracts the bot calls.
_ABI_DIR = Path(__file__).resolve().parents[2] / "contracts"
_ABI_FILES = (
    "CollateralVaultFactory.json",
    "VaultManager.json",
    "EulerCollateralVault.json",
    "AaveV3CollateralVault.json",
    "TwyneLiquidator.json",
    "TwyneAaveLiquidator.json",
)

# A 4-byte selector as it appears inside a web3 exception message.
_SELECTOR_RE = re.compile(r"0x[0-9a-fA-F]{8}\b")


def _build_selector_map() -> Dict[str, str]:
    selectors: Dict[str, str] = {}
    for name in _ABI_FILES:
        path = _ABI_DIR / name
        try:
            abi = json.loads(path.read_text(encoding="utf-8"))["abi"]
        except Exception:  # missing, unreadable or unexpected shape — stay silent
            continue
        for entry in abi:
            if entry.get("type") != "error":
                continue
            signature = entry["name"] + "(" + ",".join(i["type"] for i in entry.get("inputs", [])) + ")"
            selectors.setdefault("0x" + keccak(text=signature)[:4].hex(), signature)
    return selectors


ERROR_SELECTORS: Dict[str, str] = _build_selector_map()


def error_name(selector: str) -> Optional[str]:
    """Return the error signature for a 4-byte selector, or None if it is unknown."""
    return ERROR_SELECTORS.get(selector.lower())


def describe_revert(error: object) -> str:
    """Return a short ` [<Signature>]` suffix for the first known error selector.

    Returns an empty string when the text holds no known selector, so callers can
    append the result to a log message unconditionally.
    """
    names = []
    for match in _SELECTOR_RE.findall(str(error)):
        signature = error_name(match)
        if signature and signature not in names:
            names.append(signature)
    return f" [{', '.join(names)}]" if names else ""
