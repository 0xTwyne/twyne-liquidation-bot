"""Load forge build artifacts (ABI + bytecode) from ``out/`` for the e2e harness.

Run ``forge build`` before the e2e suite so these artifacts exist (DEV-579).
"""

import json
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_OUT_DIR = os.path.join(_REPO_ROOT, "out")


def load_artifact(name: str) -> dict:
    """Load ``out/<name>.sol/<name>.json`` (the forge artifact for contract ``name``)."""
    path = os.path.join(_OUT_DIR, f"{name}.sol", f"{name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"forge artifact not found: {path} — run `forge build` first")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def abi_of(name: str) -> list:
    return load_artifact(name)["abi"]


def creation_bytecode(name: str) -> str:
    """Creation (constructor) bytecode hex (0x-prefixed) for deploying ``name``."""
    return load_artifact(name)["bytecode"]["object"]


def deployed_bytecode(name: str) -> str:
    """Runtime/deployed bytecode hex (0x-prefixed) — for anvil_setCode (vm.etch)."""
    return load_artifact(name)["deployedBytecode"]["object"]
