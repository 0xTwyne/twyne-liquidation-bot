"""
Tests for the 1inch swapper slippage math.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.liquidation import swap_1inch
from app.liquidation.swap_1inch import OneInchSwapper, _slippage_for_min_return

# Mirrors the DEV-508 incident: USDC (6dp) target, ~315.41 flashloan repayment.
MAX_REPAY = 315_410_021
MIN_RETURN = MAX_REPAY + 1  # max_repay + MIN_LIQUIDATION_PROFIT
EOA = "0xA94D9d3b3f2A69559E89ea05B91940166382E23a"
SRC_TOKEN = "0x4200000000000000000000000000000000000006"
DST_TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_comfortable_margin_keeps_default_slippage():
    # Expected output far above the requirement -> keep the usual protection.
    assert _slippage_for_min_return(400_000_000, MIN_RETURN, default_slippage=1.0) == 1.0


def test_thin_margin_tightens_slippage_to_pin_the_floor():
    # ~0.35% headroom -> slippage tightened below the 1% default so minReturn ~= required.
    expected = 316_500_000
    slippage = _slippage_for_min_return(expected, MIN_RETURN, default_slippage=1.0)
    assert slippage == pytest.approx((expected - MIN_RETURN) / expected * 100.0)
    assert 0 < slippage < 1.0
    # The implied floor must cover the repayment requirement.
    assert expected * (1 - slippage / 100.0) >= MIN_RETURN - 1


def test_underwater_returns_zero_slippage():
    # Quote below the requirement -> tightest possible floor (will revert downstream).
    assert _slippage_for_min_return(314_000_000, MIN_RETURN, default_slippage=1.0) == 0.0


def test_zero_or_missing_expected_falls_back_to_default():
    assert _slippage_for_min_return(0, MIN_RETURN, default_slippage=1.0) == 1.0


def test_exact_match_returns_zero():
    assert _slippage_for_min_return(MIN_RETURN, MIN_RETURN, default_slippage=1.0) == 0.0


def _config():
    eth = MagicMock()
    eth.gas_price = 10
    eth.get_transaction_count.return_value = 7
    eth.estimate_gas.return_value = 21000
    eth.account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"signed")
    eth.send_raw_transaction.return_value = MagicMock(hex=lambda: "0xabc")
    eth.wait_for_transaction_receipt.return_value = SimpleNamespace(status=1)
    w3 = SimpleNamespace(eth=eth)
    return SimpleNamespace(
        w3=w3,
        CHAIN_ID=8453,
        ONEINCH_API_KEY="test-key",
        LIQUIDATOR_EOA=EOA,
        LIQUIDATOR_EOA_PRIVATE_KEY="0x" + "11" * 32,
        ERC20_ABI_PATH="erc20.json",
    )


def test_get_swap_transaction_defaults_missing_recipient_to_liquidator(monkeypatch):
    captured = {}

    def fake_request(url, headers, params):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return {"tx": {"to": EOA, "data": "0x", "value": "0", "gasPrice": "1", "gas": "21000"}}

    monkeypatch.setattr(swap_1inch.time, "sleep", lambda *_: None)
    monkeypatch.setattr(swap_1inch, "make_api_request", fake_request)

    tx = OneInchSwapper(_config()).get_swap_transaction(SRC_TOKEN, DST_TOKEN, 100, externallyLiquidated=False)

    assert tx["to"] == EOA
    assert captured["params"]["from"] == EOA
    assert captured["params"]["receiver"] == EOA


def test_approve_token_sends_web3_v7_raw_transaction(monkeypatch):
    cfg = _config()
    monkeypatch.setattr(swap_1inch.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        swap_1inch,
        "make_api_request",
        lambda *a, **k: {"to": EOA, "data": "0x", "value": "0"},
    )

    assert OneInchSwapper(cfg).approve_token(SRC_TOKEN) == "0xabc"
    cfg.w3.eth.send_raw_transaction.assert_called_once_with(b"signed")


def test_execute_swap_sends_web3_v7_raw_transaction(monkeypatch):
    cfg = _config()
    swapper = OneInchSwapper(cfg)
    monkeypatch.setattr(swapper, "check_allowance", lambda *a, **k: True)
    monkeypatch.setattr(
        swapper,
        "get_swap_transaction",
        lambda *a, **k: {"to": EOA, "data": "0x", "value": "0", "gasPrice": "1", "gas": "21000"},
    )

    tx_hash, swap_data = swapper.execute_swap(SRC_TOKEN, DST_TOKEN, 100)

    assert tx_hash == "0xabc"
    assert swap_data["to"] == EOA
    cfg.w3.eth.send_raw_transaction.assert_called_once_with(b"signed")


def _swap_data_hex(min_return: int) -> str:
    data = bytearray(228)
    data[196:228] = min_return.to_bytes(32, "big")
    return "0x" + data.hex()


def test_build_swap_internal_uses_min_return_quote_and_decodes(monkeypatch):
    swapper = OneInchSwapper(_config())
    captured = {}

    def fake_with_min_return(src, dst, amount, recipient, min_return, default_slippage=1.0):
        captured["args"] = (src, dst, amount, recipient, min_return, default_slippage)
        return {"data": _swap_data_hex(MIN_RETURN)}

    monkeypatch.setattr(swapper, "get_swap_transaction_with_min_return", fake_with_min_return)
    monkeypatch.setattr(swapper, "get_swap_transaction", MagicMock())

    result = swapper.build_swap(SRC_TOKEN, DST_TOKEN, 100, EOA, MIN_RETURN, externally_liquidated=False)

    assert result is not None
    assert result.min_return == MIN_RETURN
    assert result.calldata == bytes.fromhex(_swap_data_hex(MIN_RETURN).replace("0x", ""))
    assert captured["args"] == (SRC_TOKEN, DST_TOKEN, 100, EOA, MIN_RETURN, 1.0)
    swapper.get_swap_transaction.assert_not_called()


def test_build_swap_external_uses_exact_in_zero_slippage_and_decodes(monkeypatch):
    swapper = OneInchSwapper(_config())
    captured = {}

    def fake_swap_tx(src, dst, amount, externally_liquidated, slippage, recipient):
        captured["args"] = (src, dst, amount, externally_liquidated, slippage, recipient)
        return {"data": _swap_data_hex(MIN_RETURN)}

    monkeypatch.setattr(swapper, "get_swap_transaction", fake_swap_tx)
    monkeypatch.setattr(swapper, "get_swap_transaction_with_min_return", MagicMock())

    result = swapper.build_swap(SRC_TOKEN, DST_TOKEN, 100, EOA, MIN_RETURN, externally_liquidated=True)

    assert result is not None
    assert result.min_return == MIN_RETURN
    assert captured["args"] == (SRC_TOKEN, DST_TOKEN, 100, True, 0, EOA)
    swapper.get_swap_transaction_with_min_return.assert_not_called()


def test_build_swap_non_positive_amount_returns_none():
    swapper = OneInchSwapper(_config())
    assert swapper.build_swap(SRC_TOKEN, DST_TOKEN, 0, EOA, MIN_RETURN, externally_liquidated=False) is None


def test_build_swap_returns_none_when_1inch_returns_no_data(monkeypatch):
    swapper = OneInchSwapper(_config())
    monkeypatch.setattr(swapper, "get_swap_transaction_with_min_return", lambda *a, **k: None)
    assert swapper.build_swap(SRC_TOKEN, DST_TOKEN, 100, EOA, MIN_RETURN, externally_liquidated=False) is None


def test_build_swap_returns_none_on_short_calldata(monkeypatch):
    # Calldata shorter than 228 bytes cannot carry the minReturn slot -> skip.
    swapper = OneInchSwapper(_config())
    monkeypatch.setattr(swapper, "get_swap_transaction_with_min_return", lambda *a, **k: {"data": "0x1234"})
    assert swapper.build_swap(SRC_TOKEN, DST_TOKEN, 100, EOA, MIN_RETURN, externally_liquidated=False) is None


def test_cli_aborts_unless_confirmation_is_yes(monkeypatch, capsys):
    fake_swapper = MagicMock()
    fake_swapper.get_swap_quote.return_value = {"dstAmount": "99"}
    monkeypatch.setattr(swap_1inch, "load_chain_config", lambda chain_id: SimpleNamespace(CHAIN_ID=chain_id))
    monkeypatch.setattr(swap_1inch, "OneInchSwapper", lambda config: fake_swapper)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "swap_1inch.py",
            "--src-token",
            SRC_TOKEN,
            "--dst-token",
            DST_TOKEN,
            "--amount-wei",
            "100",
            "--recipient",
            EOA,
        ],
    )

    assert swap_1inch.main() == 1
    fake_swapper.get_swap_quote.assert_called_once_with(SRC_TOKEN, DST_TOKEN, 100, 1.0)
    fake_swapper.execute_swap.assert_not_called()
    assert "Swap aborted." in capsys.readouterr().out
