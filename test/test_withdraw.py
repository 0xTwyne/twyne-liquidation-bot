from types import SimpleNamespace
from unittest.mock import MagicMock

from app.liquidation import withdraw

EOA = "0xA94D9d3b3f2A69559E89ea05B91940166382E23a"


def _config():
    eth = MagicMock()
    eth.get_transaction_count.return_value = 3
    eth.account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"signed")
    eth.send_raw_transaction.return_value = MagicMock(hex=lambda: "0xdef")
    eth.wait_for_transaction_receipt.return_value = SimpleNamespace(gasUsed=21000)
    return SimpleNamespace(
        w3=SimpleNamespace(eth=eth),
        CHAIN_ID=8453,
        LIQUIDATOR_EOA=EOA,
        LIQUIDATOR_EOA_PRIVATE_KEY="0x" + "11" * 32,
        CVAULT_ABI_PATH="cvault.json",
    )


def test_withdraw_collateral_sends_web3_v7_raw_transaction(monkeypatch):
    cfg = _config()
    vault = MagicMock()
    vault.functions.totalAssets.return_value.call.return_value = 100
    vault.functions.maxWithdraw.return_value.call.return_value = 100
    vault.functions.borrower.return_value.call.return_value = EOA
    vault.functions.redeemUnderlying.return_value.build_transaction.return_value = {"nonce": 3}
    monkeypatch.setattr(withdraw, "create_contract_instance", lambda *a, **k: vault)
    monkeypatch.setattr(withdraw, "get_eip1559_fees", lambda *a, **k: SimpleNamespace(to_tx_fields=lambda: {}))
    monkeypatch.setattr(withdraw.time, "sleep", lambda *_: None)

    tx_hash, tx_receipt = withdraw.withdraw_collateral(EOA, cfg)

    assert tx_hash == "0xdef"
    assert tx_receipt.gasUsed == 21000
    cfg.w3.eth.send_raw_transaction.assert_called_once_with(b"signed")


def test_withdraw_main_processes_all_vaults_before_returning(monkeypatch, capsys):
    cfg = _config()
    calls = []
    monkeypatch.setattr(withdraw, "load_chain_config", lambda chain_id: cfg)
    monkeypatch.setattr(withdraw, "get_user_collateral_vaults", lambda config: ["vault-a", "vault-b", "vault-c"])

    def fake_withdraw(vault, config):
        calls.append(vault)
        if vault == "vault-b":
            return None, None
        return f"0x{vault}", SimpleNamespace()

    monkeypatch.setattr(withdraw, "withdraw_collateral", fake_withdraw)

    assert withdraw.main() == 1
    assert calls == ["vault-a", "vault-b", "vault-c"]
    output = capsys.readouterr().out
    assert "Withdrawal successful for vault-a" in output
    assert "Withdrawal failed for vault-b" in output
    assert "Withdrawal successful for vault-c" in output
