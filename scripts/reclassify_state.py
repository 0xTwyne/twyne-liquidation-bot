#!/usr/bin/env python3
"""
Post-deploy verification + repair for `state/<Chain>_state.json`.

After deploying the `detect_protocol` fix, run this script once per chain to
confirm every persisted CV's `protocol` field matches what the corrected
detector returns from chain. Mismatched entries are flagged; with `--apply`
the script rewrites the state JSON in place.

Usage:
    # Dry-run audit
    python scripts/reclassify_state.py --chain 1 --state-file state/Ethereum_state.json

    # Repair
    python scripts/reclassify_state.py --chain 1 --state-file state/Ethereum_state.json --apply

Exits 0 if state agrees with chain, 1 otherwise (so the script doubles as
a deploy gate).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.liquidation.config_loader import load_chain_config
from app.liquidation.vaults.registry import detect_protocol


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify CV protocol classification in a bot state file.")
    parser.add_argument("--chain", type=int, required=True, help="Chain ID (e.g. 1)")
    parser.add_argument("--state-file", required=True, help="Path to <Chain>_state.json")
    parser.add_argument("--apply", action="store_true", help="Rewrite the state file in place")
    parser.add_argument("--log-level", default="WARNING", help="DEBUG / INFO / WARNING (default WARNING)")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level)
    # Silence the per-call info logs from detect_protocol unless the operator asked for them.
    logging.getLogger("app.liquidation.vaults.registry").setLevel(args.log_level)

    state_path = Path(args.state_file)
    state = json.loads(state_path.read_text())
    accounts = state.get("accounts", {})

    config = load_chain_config(args.chain)

    mismatches: list[tuple[str, str, str]] = []
    errors: list[tuple[str, str]] = []

    for addr, data in accounts.items():
        stored = data.get("protocol", "euler")
        try:
            actual = detect_protocol(addr, config)
        except Exception as exc:
            errors.append((addr, str(exc)))
            continue
        if actual != stored:
            mismatches.append((addr, stored, actual))

    print(f"Checked {len(accounts)} CVs in {state_path}")
    print(f"  Mismatches: {len(mismatches)}")
    print(f"  RPC errors: {len(errors)}")

    for addr, stored, actual in mismatches:
        print(f"  MISMATCH {addr}: stored={stored} actual={actual}")
    for addr, msg in errors:
        print(f"  ERROR    {addr}: {msg}")

    if mismatches and args.apply:
        for addr, _stored, actual in mismatches:
            accounts[addr]["protocol"] = actual
        state["accounts"] = accounts
        state_path.write_text(json.dumps(state, indent=2))
        print(f"Rewrote {state_path} with corrected protocol fields.")

    # Non-zero exit if anything is wrong AND we weren't asked to fix it,
    # so the script can gate a deploy.
    if (mismatches and not args.apply) or errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
