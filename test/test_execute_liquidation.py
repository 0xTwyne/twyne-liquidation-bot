"""
Tests for BaseLiquidator.execute_liquidation retry semantics.

Mocks the web3 surface and exercises the four post-TimeExhausted branches:
- success on first attempt
- success after one TimeExhausted (retry with bumped fees)
- late receipt (mined while we were waiting; nonce advanced)
- terminal failure after all attempts exhausted
"""

import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

# Force a small attempt budget for tests so they run fast and the assertions are clear.
os.environ.setdefault("LIQ_MAX_SUBMIT_ATTEMPTS", "3")
os.environ.setdefault("LIQ_FEE_BUMP_FACTOR", "1.50")
os.environ.setdefault("LIQ_RECEIPT_TIMEOUT_SECS", "60")

from web3.exceptions import TimeExhausted, TransactionNotFound  # noqa: E402

from app.liquidation.vaults import base_vault  # noqa: E402
from app.liquidation.vaults.base_vault import BaseLiquidator  # noqa: E402

EOA = "0xA94D9d3b3f2A69559E89ea05B91940166382E23a"


def _make_config(send_side_effects, receipt_side_effects, tx_count_side_effects, get_receipt_side_effects=None):
    """Build a minimal mock ChainConfig with the call surface execute_liquidation needs."""
    eth = MagicMock()
    eth.account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"signed")
    eth.send_raw_transaction.side_effect = send_side_effects
    eth.wait_for_transaction_receipt.side_effect = receipt_side_effects
    eth.get_transaction_count.side_effect = tx_count_side_effects
    if get_receipt_side_effects is not None:
        eth.get_transaction_receipt.side_effect = get_receipt_side_effects
    w3 = MagicMock()
    w3.eth = eth
    return SimpleNamespace(w3=w3, LIQUIDATOR_EOA=EOA, LIQUIDATOR_EOA_PRIVATE_KEY="0x" + "11" * 32)


def _tx(nonce=29, max_fee=10**10, tip=10**9):
    return {
        "nonce": nonce,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": tip,
        "chainId": 1,
        "from": EOA,
        "gas": 100000,
        "to": "0x0",
        "data": "0x",
        "value": 0,
    }


def test_success_on_first_attempt(monkeypatch):
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)  # don't actually sleep in tests
    receipt = {"blockNumber": 1, "status": 1, "gasUsed": 21000, "effectiveGasPrice": 5}
    cfg = _make_config(
        send_side_effects=[MagicMock(hex=lambda: "0xaaa")],
        receipt_side_effects=[receipt],
        tx_count_side_effects=[29],
    )
    h, r = BaseLiquidator.execute_liquidation(_tx(), cfg)
    assert r == receipt
    assert h == "0xaaa"
    assert cfg.w3.eth.send_raw_transaction.call_count == 1
    signed_tx = cfg.w3.eth.account.sign_transaction.call_args.args[0]
    assert signed_tx["nonce"] == 29


def test_retry_with_bumped_fees_then_success(monkeypatch):
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)
    receipt = {"blockNumber": 2, "status": 1, "gasUsed": 21000, "effectiveGasPrice": 5}
    cfg = _make_config(
        send_side_effects=[MagicMock(hex=lambda: "0xaaa"), MagicMock(hex=lambda: "0xbbb")],
        receipt_side_effects=[TimeExhausted("timeout"), receipt],
        tx_count_side_effects=[29, 29],  # pending nonce, then latest nonce after timeout
    )
    h, r = BaseLiquidator.execute_liquidation(_tx(nonce=29, max_fee=10**10, tip=10**9), cfg)
    assert r == receipt
    assert h == "0xbbb"
    assert cfg.w3.eth.send_raw_transaction.call_count == 2

    # Second sign call must use bumped fees.
    second_call_tx = cfg.w3.eth.account.sign_transaction.call_args_list[1].args[0]
    assert second_call_tx["maxFeePerGas"] == int(10**10 * 1.50)
    assert second_call_tx["maxPriorityFeePerGas"] == int(10**9 * 1.50)
    assert second_call_tx["nonce"] == 29  # same nonce: replacement, not a new tx


def test_late_receipt_after_timeout_returns_mined_tx(monkeypatch):
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)
    late_receipt = {"blockNumber": 3, "status": 1, "gasUsed": 21000, "effectiveGasPrice": 5}
    cfg = _make_config(
        send_side_effects=[MagicMock(hex=lambda: "0xccc")],
        receipt_side_effects=[TimeExhausted("timeout")],
        tx_count_side_effects=[29, 30],  # pending nonce, then latest nonce ADVANCED → tx mined late
        get_receipt_side_effects=[late_receipt],
    )
    h, r = BaseLiquidator.execute_liquidation(_tx(nonce=29), cfg)
    assert r == late_receipt
    assert h == "0xccc"
    assert cfg.w3.eth.send_raw_transaction.call_count == 1  # no retry — already mined


def test_late_receipt_lookup_fails_returns_none(monkeypatch):
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)
    cfg = _make_config(
        send_side_effects=[MagicMock(hex=lambda: "0xddd")],
        receipt_side_effects=[TimeExhausted("timeout")],
        tx_count_side_effects=[29, 30],  # pending nonce, then latest nonce advanced but our hash isn't findable
        get_receipt_side_effects=[TransactionNotFound("not found")],
    )
    h, r = BaseLiquidator.execute_liquidation(_tx(nonce=29), cfg)
    assert (h, r) == (None, None)


def test_terminal_timeout_after_max_attempts(monkeypatch):
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)
    monkeypatch.setattr(base_vault, "post_error_notification", lambda *a, **k: None)
    cfg = _make_config(
        send_side_effects=[MagicMock(hex=lambda: f"0x{i}") for i in range(3)],
        receipt_side_effects=[TimeExhausted("t")] * 3,
        tx_count_side_effects=[29, 29, 29, 29],  # pending nonce, then latest never advances
    )
    h, r = BaseLiquidator.execute_liquidation(_tx(nonce=29), cfg)
    assert (h, r) == (None, None)
    assert cfg.w3.eth.send_raw_transaction.call_count == 3


def test_send_raw_value_error_is_terminal(monkeypatch):
    """nonce-too-low / replacement-underpriced: don't retry, just bail."""
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)
    cfg = _make_config(
        send_side_effects=[ValueError("nonce too low")],
        receipt_side_effects=[],
        tx_count_side_effects=[29],
    )
    h, r = BaseLiquidator.execute_liquidation(_tx(), cfg)
    assert (h, r) == (None, None)
    assert cfg.w3.eth.send_raw_transaction.call_count == 1


def test_concurrent_liquidations_get_sequential_nonces(monkeypatch):
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)
    receipts = [{"blockNumber": i, "status": 1, "gasUsed": 21000, "effectiveGasPrice": 5} for i in (1, 2)]
    cfg = _make_config(
        send_side_effects=[MagicMock(hex=lambda: "0xaaa"), MagicMock(hex=lambda: "0xbbb")],
        receipt_side_effects=receipts,
        tx_count_side_effects=[29],
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: BaseLiquidator.execute_liquidation(_tx(), cfg), range(2)))

    assert all(receipt["status"] == 1 for _tx_hash, receipt in results)
    signed_nonces = [call.args[0]["nonce"] for call in cfg.w3.eth.account.sign_transaction.call_args_list]
    assert signed_nonces == [29, 30]
    assert cfg.w3.eth.get_transaction_count.call_count == 1


def test_send_rejection_resyncs_next_liquidation_nonce(monkeypatch):
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)
    receipt = {"blockNumber": 1, "status": 1, "gasUsed": 21000, "effectiveGasPrice": 5}
    cfg = _make_config(
        send_side_effects=[ValueError("nonce too low"), MagicMock(hex=lambda: "0xaaa")],
        receipt_side_effects=[receipt],
        tx_count_side_effects=[29, 31],
    )

    assert BaseLiquidator.execute_liquidation(_tx(), cfg) == (None, None)
    h, r = BaseLiquidator.execute_liquidation(_tx(), cfg)

    assert h == "0xaaa"
    assert r == receipt
    signed_nonces = [call.args[0]["nonce"] for call in cfg.w3.eth.account.sign_transaction.call_args_list]
    assert signed_nonces == [29, 31]


def test_unexpected_error_after_send_resyncs_next_liquidation_nonce(monkeypatch):
    """A non-TimeExhausted/non-ValueError error during the receipt wait (e.g. an RPC
    connection drop) must not strand the local nonce counter: the next liquidation
    re-seeds from chain so a dropped tx can't block all subsequent sends."""
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)
    monkeypatch.setattr(base_vault, "post_error_notification", lambda *a, **k: None)
    receipt = {"blockNumber": 1, "status": 1, "gasUsed": 21000, "effectiveGasPrice": 5}
    cfg = _make_config(
        # First send broadcasts at nonce 29, then the receipt wait blows up with a
        # generic connection error; second send succeeds at the re-seeded nonce.
        send_side_effects=[MagicMock(hex=lambda: "0xaaa"), MagicMock(hex=lambda: "0xbbb")],
        receipt_side_effects=[ConnectionError("rpc dropped"), receipt],
        tx_count_side_effects=[29, 31],  # initial seed, then re-seed after invalidate()
    )

    assert BaseLiquidator.execute_liquidation(_tx(), cfg) == (None, None)
    h, r = BaseLiquidator.execute_liquidation(_tx(), cfg)

    assert (h, r) == ("0xbbb", receipt)
    signed_nonces = [call.args[0]["nonce"] for call in cfg.w3.eth.account.sign_transaction.call_args_list]
    assert signed_nonces == [29, 31]
    # invalidate() forced a fresh chain read for the second send.
    assert cfg.w3.eth.get_transaction_count.call_count == 2


def test_earlier_attempt_mined_reported_as_success(monkeypatch):
    """An earlier replacement attempt that mined (not the last one) must be found and
    reported as success, not as a failure. Scenario: 2 sends at nonce 29; second
    send triggers TimeExhausted; nonce has advanced → we scan both hashes; only the
    first hash has a receipt."""
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)
    monkeypatch.setattr(base_vault, "post_error_notification", lambda *a, **k: None)

    first_hash = MagicMock(hex=lambda: "0xfirst")
    second_hash = MagicMock(hex=lambda: "0xsecond")
    early_receipt = {"blockNumber": 5, "status": 1, "gasUsed": 21000, "effectiveGasPrice": 5}

    cfg = _make_config(
        send_side_effects=[first_hash, second_hash],
        receipt_side_effects=[TimeExhausted("timeout1"), TimeExhausted("timeout2")],
        # pending nonce twice (initial seed for each attempt), then latest=30 (nonce mined)
        tx_count_side_effects=[29, 29, 30],
        # first_hash has a receipt; second_hash is not found
        get_receipt_side_effects=[early_receipt, TransactionNotFound("not found")],
    )

    h, r = BaseLiquidator.execute_liquidation(_tx(nonce=29), cfg)

    assert h == "0xfirst", "earlier mined hash must be returned"
    assert r == early_receipt, "receipt of earlier mined tx must be returned"
    assert cfg.w3.eth.send_raw_transaction.call_count == 2


def test_value_error_after_earlier_attempt_mined_reported_as_success(monkeypatch):
    """A 'nonce too low' ValueError on the second send means the first attempt already
    mined. Must scan submitted hashes and return success rather than (None, None)."""
    monkeypatch.setattr(base_vault.time, "sleep", lambda *_: None)

    first_hash = MagicMock(hex=lambda: "0xfirstval")
    mined_receipt = {"blockNumber": 7, "status": 1, "gasUsed": 21000, "effectiveGasPrice": 5}

    cfg = _make_config(
        send_side_effects=[first_hash, ValueError("nonce too low")],
        receipt_side_effects=[TimeExhausted("timeout")],
        tx_count_side_effects=[29, 29],
        get_receipt_side_effects=[mined_receipt],
    )

    h, r = BaseLiquidator.execute_liquidation(_tx(nonce=29), cfg)

    assert h == "0xfirstval", "mined hash must be returned on nonce-too-low ValueError"
    assert r == mined_receipt
