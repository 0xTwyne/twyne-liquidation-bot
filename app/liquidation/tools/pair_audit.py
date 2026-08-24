"""
One-shot audit tool: groups every tracked CV by its (collateral, debt) pair
and reports counts by correlation status.

Reads `state/<ChainName>_state.json` for the list of tracked CV addresses,
then re-instantiates each via the bot's own vault class — so the on-chain
asset lookups and correlation classification are exactly the same logic the
running bot uses. No new RPC contracts to maintain.

Usage:
    python -m app.liquidation.tools.pair_audit --chain 1
    python -m app.liquidation.tools.pair_audit --chain 1 --state-dir state
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from web3 import Web3

from app.liquidation.config_loader import load_chain_config
from app.liquidation.vaults.registry import detect_protocol, get_vault_class_for_protocol


def _symbol(w3, address: str) -> str:
    """Best-effort symbol() lookup; falls back to short-address on failure."""
    abi = [
        {
            "inputs": [],
            "name": "symbol",
            "outputs": [{"type": "string"}],
            "stateMutability": "view",
            "type": "function",
        }
    ]
    try:
        return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi).functions.symbol().call()
    except Exception:
        return f"{address[:6]}…{address[-4:]}"


def _classify_one(address: str, recorded_protocol: str, config):
    """
    Instantiate the CV and return (collateral_addr, debt_addr, is_correlated).
    Falls back to detect_protocol() if state has no protocol tag.
    """
    protocol = recorded_protocol or detect_protocol(address, config)
    vault_class = get_vault_class_for_protocol(protocol)
    vault = vault_class(address, config)
    return (
        Web3.to_checksum_address(vault.underlying_asset_address),
        Web3.to_checksum_address(vault.target_asset),
        bool(vault.is_correlated),
    )


def audit_chain(chain_id: int, state_dir: Path) -> int:
    config = load_chain_config(chain_id)
    chain_name = config.CHAIN_NAME
    state_path = state_dir / f"{chain_name}_state.json"

    if not state_path.exists():
        print(f"  (no state file at {state_path} — nothing to audit)")
        return 0

    state = json.loads(state_path.read_text())
    accounts = state.get("accounts", {})

    counts_correlated = Counter()
    counts_uncorrelated = Counter()
    failed = []

    for addr, data in accounts.items():
        try:
            collateral, debt, is_correlated = _classify_one(addr, data.get("protocol", "euler"), config)
        except Exception as ex:
            failed.append((addr, str(ex)))
            continue
        key = (collateral, debt)
        if is_correlated:
            counts_correlated[key] += 1
        else:
            counts_uncorrelated[key] += 1

    # Resolve symbols lazily once per unique address to keep RPC calls down.
    seen_addresses = set()
    for k in list(counts_correlated.keys()) + list(counts_uncorrelated.keys()):
        seen_addresses.update(k)
    symbol_cache = {a: _symbol(config.w3, a) for a in seen_addresses}

    def fmt_pair(pair):
        c, d = pair
        return f"{symbol_cache[c]:>20} / {symbol_cache[d]:<10}  ({c} / {d})"

    print(f"\nChain {chain_id} ({chain_name}):")
    print("  CORRELATED:")
    if counts_correlated:
        for pair, n in sorted(counts_correlated.items(), key=lambda x: -x[1]):
            print(f"    {n:4d}  {fmt_pair(pair)}")
    else:
        print("    (none)")
    total_c = sum(counts_correlated.values())
    print(f"    -- subtotal: {total_c}")

    print("  NON-CORRELATED:")
    if counts_uncorrelated:
        for pair, n in sorted(counts_uncorrelated.items(), key=lambda x: -x[1]):
            print(f"    {n:4d}  {fmt_pair(pair)}")
    else:
        print("    (none)")
    total_u = sum(counts_uncorrelated.values())
    print(f"    -- subtotal: {total_u}")

    print(f"  TOTAL: {total_c + total_u} CVs  ({total_c} correlated, {total_u} non-correlated)")

    if failed:
        print(f"\n  FAILED to classify {len(failed)} CV(s):")
        for addr, ex in failed:
            print(f"    {addr}: {ex}")

    return len(failed)


def main():
    parser = argparse.ArgumentParser(
        description="Group tracked CVs by (collateral, debt) pair and report counts.",
    )
    parser.add_argument("--chain", type=int, required=True, help="Chain ID (e.g. 1)")
    parser.add_argument("--state-dir", default="state", help="Directory holding <Chain>_state.json")
    args = parser.parse_args()

    state_dir = Path(args.state_dir).resolve()
    failed = audit_chain(args.chain, state_dir)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
