"""Anvil mainnet-fork lifecycle + cheatcode helpers for the e2e harness (DEV-579).

Wraps an ``anvil --fork-url ...`` subprocess and exposes the anvil RPC cheatcodes
the state seeder needs — the live-anvil equivalents of the forge cheatcodes used by
``test/LiquidationStateBuilder.sol``:

    vm.prank / startPrank   -> anvil_impersonateAccount + eth_sendTransaction
    vm.etch                 -> anvil_setCode
    deal (ERC20)            -> anvil_setStorageAt (balance-slot probe)
    vm.deal (ETH)           -> anvil_setBalance
    vm.warp                 -> evm_increaseTime + evm_mine
    vm.snapshot / revert    -> evm_snapshot / evm_revert
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from typing import Any, List, Optional

from eth_abi import encode as abi_encode
from web3 import Web3

# Minimal ERC20 ABI for balance probing / reads.
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

_ANVIL_BIN = shutil.which("anvil") or os.path.expanduser("~/.config/.foundry/bin/anvil")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def encode_call(selector_sig: str, types: List[str], args: list) -> str:
    """Return 0x-prefixed calldata: 4-byte selector of ``selector_sig`` + abi-encoded args.

    ``selector_sig`` is the canonical Solidity signature used for the selector, e.g.
    ``"borrow(uint256,address)"`` or ``"batch((address,address,uint256,bytes)[])"``.
    """
    selector = Web3.keccak(text=selector_sig)[:4]
    return "0x" + (selector + abi_encode(types, args)).hex()


class AnvilFork:
    """A running anvil mainnet fork with cheatcode helpers."""

    def __init__(self, fork_url: str, block: int, port: Optional[int] = None):
        self.fork_url = fork_url
        self.block = block
        self.port = port or _free_port()
        self.rpc_url = f"http://127.0.0.1:{self.port}"
        self._proc: Optional[subprocess.Popen] = None
        self.w3: Optional[Web3] = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, ready_timeout: float = 90.0) -> "AnvilFork":
        self._proc = subprocess.Popen(
            [
                _ANVIL_BIN,
                "--fork-url",
                self.fork_url,
                "--fork-block-number",
                str(self.block),
                "--port",
                str(self.port),
                "--chain-id",
                "1",
                # Ride out transient upstream archive-RPC timeouts (e.g. drpc free-tier
                # 408s under load): anvil retries the fork fetch instead of surfacing it.
                "--retries",
                "10",
                "--timeout",
                "60000",
                "--silent",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 120}))
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError("anvil exited during startup")
            try:
                if self.w3.is_connected() and self.w3.eth.block_number >= self.block:
                    return self
            except Exception:
                pass
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"anvil did not become ready within {ready_timeout}s")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    # -- raw rpc -----------------------------------------------------------
    def rpc(self, method: str, params: list) -> Any:
        resp = self.w3.provider.make_request(method, params)
        if "error" in resp:
            raise RuntimeError(f"{method} failed: {resp['error']}")
        return resp.get("result")

    # -- cheatcodes --------------------------------------------------------
    def impersonate(self, addr: str) -> None:
        self.rpc("anvil_impersonateAccount", [addr])

    def stop_impersonate(self, addr: str) -> None:
        self.rpc("anvil_stopImpersonatingAccount", [addr])

    def set_balance(self, addr: str, wei: int) -> None:
        self.rpc("anvil_setBalance", [addr, hex(wei)])

    def set_code(self, addr: str, code_hex: str) -> None:
        self.rpc("anvil_setCode", [addr, code_hex])

    def set_storage(self, addr: str, slot_hex: str, value_hex: str) -> None:
        self.rpc("anvil_setStorageAt", [addr, slot_hex, value_hex])

    def get_storage(self, addr: str, slot_hex: str) -> str:
        return self.rpc("eth_getStorageAt", [addr, slot_hex, "latest"])

    def warp(self, seconds: int) -> None:
        self.rpc("evm_increaseTime", [seconds])
        self.rpc("evm_mine", [])

    def snapshot(self) -> str:
        return self.rpc("evm_snapshot", [])

    def revert(self, snap_id: str) -> bool:
        return self.rpc("evm_revert", [snap_id])

    # -- transactions ------------------------------------------------------
    def send(self, frm: str, to: str, data: str = "0x", value: int = 0, gas: int = 12_000_000):
        """Send a tx from an impersonated account, wait, and assert success."""
        self.impersonate(frm)
        self.set_balance(frm, max(self._balance(frm), 10**20))
        try:
            tx = {"from": frm, "to": to, "data": data, "value": hex(value), "gas": hex(gas)}
            tx_hash = self.rpc("eth_sendTransaction", [tx])
        finally:
            self.stop_impersonate(frm)
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if rcpt["status"] != 1:
            raise RuntimeError(f"tx reverted: from={frm} to={to} data={data[:10]}")
        return rcpt

    def deploy(self, deployer: str, creation_hex: str, gas: int = 12_000_000) -> str:
        """Deploy a contract from creation bytecode; return the deployed address."""
        self.impersonate(deployer)
        self.set_balance(deployer, 10**21)
        try:
            tx = {"from": deployer, "data": creation_hex, "gas": hex(gas)}
            tx_hash = self.rpc("eth_sendTransaction", [tx])
        finally:
            self.stop_impersonate(deployer)
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if rcpt["status"] != 1 or not rcpt["contractAddress"]:
            raise RuntimeError("contract deploy failed")
        return Web3.to_checksum_address(rcpt["contractAddress"])

    def _balance(self, addr: str) -> int:
        return int(self.rpc("eth_getBalance", [addr, "latest"]), 16)

    # -- ERC20 deal --------------------------------------------------------
    def deal(self, token: str, to: str, amount: int, max_slot: int = 40) -> None:
        """Set ``to``'s ERC20 balance of ``token`` to ``amount`` by probing the
        balanceOf mapping storage slot (the live-anvil equivalent of forge ``deal``).

        Works for standard ``mapping(address => uint256)`` balances (WETH, wstETH, USDC
        proxy, etc.). Raises if no slot reproduces the written balance.
        """
        token = Web3.to_checksum_address(token)
        to = Web3.to_checksum_address(to)
        erc20 = self.w3.eth.contract(address=token, abi=_ERC20_ABI)
        value_hex = "0x" + amount.to_bytes(32, "big").hex()
        for slot in range(max_slot):
            key = "0x" + Web3.keccak(abi_encode(["address", "uint256"], [to, slot])).hex()
            prev = self.get_storage(token, key)
            self.set_storage(token, key, value_hex)
            try:
                if erc20.functions.balanceOf(to).call() == amount:
                    return
            except Exception:
                pass
            self.set_storage(token, key, prev)  # restore and keep probing
        raise RuntimeError(f"could not locate balanceOf slot for token {token}")
