from app.liquidation import notifications
from app.liquidation.position_stats import PositionStats

RAW = (
    1_500_000_000_000_000_000,  # userCollateralNative (18-dec asset) = 1.5
    3_000_000_000_000_000_000_000,  # userCollateralUsd (1e18) = $3000
    500_000_000_000_000_000,  # reservedCreditNative = 0.5
    1_000_000_000_000_000_000_000,  # reservedCreditUsd = $1000
    900_000_000,  # borrowNative (1e6 USDC) = 900
    900_000_000_000_000_000_000,  # borrowUsd = $900
    8500,  # twyneLiqLTV bps
    3000,  # twyneLTV bps
    8000,  # externalLiqLTV bps
    2250,  # externalLTV bps
    2_000_000_000_000_000_000,  # extHF 1e18 = 2.0
    2_833_333_000_000_000_000,  # inHF
    9000,  # maxTwyneLTV bps (cap) = 90%
)


def _stats():
    return PositionStats.from_raw(
        RAW,
        collateral_decimals=18,
        collateral_symbol="eWETH",
        debt_decimals=6,
        debt_symbol="USDC",
    )


def test_usd_and_ltv_scaling():
    s = _stats()
    assert s.collateral_usd == 3000.0
    assert s.borrow_usd == 900.0
    assert s.twyne_liq_ltv_pct == 85.0
    assert s.external_ltv_pct == 22.5
    assert s.max_twyne_ltv_pct == 90.0


def test_native_formatting():
    s = _stats()
    assert s.collateral_native == 1.5
    assert s.borrow_native == 900.0


def test_render_table_contains_all_rows_and_is_code_block():
    out = _stats().render_table()
    for label in ("C ", "C_LP", "B ", "LTV_t", "~LTV_t", "LTV_e", "~LTV_e", "inHF", "extHF"):
        assert label in out
    assert out.startswith("```") and out.rstrip().endswith("```")


def test_render_row_is_single_line_with_values():
    row = _stats().render_row(vault="0xabc", spy_link="http://spy")
    assert "\n" not in row.strip("\n")
    assert "3,000" in row and "Spy" in row


class _Cfg:
    CHAIN_NAME = "Ethereum"
    CHAIN_ID = 1
    NOTIFICATION_URL = "json://localhost"
    SLACK_MENTION_IDS = []


def test_unhealthy_message_includes_table(monkeypatch):
    cap = {}
    monkeypatch.setattr(notifications.Apprise, "notify", lambda self, body, title: cap.setdefault("body", body) or True)
    notifications.post_unhealthy_account_notification("0xVault", False, 0.97, 1.2, 1, 2, _Cfg(), stats=_stats())
    assert "```" in cap["body"] and "~LTV_t" in cap["body"]


def test_unhealthy_message_fallback_without_stats(monkeypatch):
    cap = {}
    monkeypatch.setattr(notifications.Apprise, "notify", lambda self, body, title: cap.setdefault("body", body) or True)
    notifications.post_unhealthy_account_notification("0xVault", False, 0.97, 1.2, 1, 2, _Cfg(), stats=None)
    assert "Internal Health Score" in cap["body"] and "```" not in cap["body"]


class _FakeAccount:
    def __init__(self, stats):
        self._s = stats

    def get_position_stats(self):
        return self._s


def test_report_uses_rows(monkeypatch):
    cap = {}
    monkeypatch.setattr(notifications.Apprise, "notify", lambda self, body, title: cap.setdefault("body", body) or True)
    monkeypatch.setattr(notifications, "get_spy_link", lambda a, c: "http://spy")

    class C(_Cfg):
        SLACK_REPORT_HEALTH_SCORE = 1.1
        TWYNE_EOA_VAULTS = []
        BORROW_VALUE_THRESHOLD = 0
        RISK_DASHBOARD_URL = "http://dash"

    rows = [("0xv1", 0.9, 1.05, 0, 10**18, 0, "WETH", _FakeAccount(_stats()))]
    notifications.post_low_health_account_report_notification(rows, C())
    assert "LTV_t=" in cap["body"]
