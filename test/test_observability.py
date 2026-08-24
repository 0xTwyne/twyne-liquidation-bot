"""Unit tests for the observability snapshot + metrics exporter.

These bypass on-chain / AWS dependencies: the AccountMonitor snapshot is tested
against hand-built account objects (via object.__new__ so __init__'s heavy
ChainConfig isn't needed), and the exporter is tested as pure functions plus a
fake CloudWatch client.
"""

import math
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.liquidation.account_monitor import AccountMonitor
from app.observability import metrics_exporter as mx


def _vault(inHF, exHF, last_checked_at):
    return SimpleNamespace(
        internal_health_score=inHF,
        external_health_score=exHF,
        last_checked_at=last_checked_at,
    )


def _monitor(accounts, *, latest_block=100, failures=0, failed_init=None, balance_wei=10**18):
    """An AccountMonitor with only the fields get_observability_snapshot reads."""
    m = AccountMonitor.__new__(AccountMonitor)
    # get_observability_snapshot now snapshots shared state under state_lock.
    m.state_lock = threading.Lock()
    m.accounts = {f"0x{i:040x}": a for i, a in enumerate(accounts)}
    m.latest_block = latest_block
    m.liquidation_failure_count = failures
    m.failed_initializations = failed_init or {}
    m._signer_balance_cache = (0.0, None)
    m.w3 = SimpleNamespace(eth=SimpleNamespace(get_balance=lambda _addr: balance_wei))
    m.config = SimpleNamespace(LIQUIDATOR_EOA="0x" + "ab" * 20)
    return m


# ---------------- AccountMonitor.get_observability_snapshot ----------------


def test_snapshot_basic_counts_and_balance():
    now = time.time()
    accounts = [
        _vault(1.5, 1.5, now - 10),  # healthy
        _vault(0.9, 1.2, now - 30),  # internal unhealthy only
        _vault(1.1, 0.95, now - 5),  # below external boundary
        _vault(math.inf, math.inf, now - 50),  # no debt
    ]
    snap = _monitor(accounts, latest_block=222, failures=3, balance_wei=2 * 10**17).get_observability_snapshot()

    assert snap["monitored_cv_count"] == 4
    assert snap["unhealthy_cv_count"] == 1  # only inHF 0.9
    assert snap["cvs_below_external_boundary"] == 1  # only exHF 0.95
    assert snap["latest_scanned_block"] == 222
    assert snap["liquidation_failure_count"] == 3
    assert snap["failed_init_count"] == 0
    assert abs(snap["signer_balance_eth"] - 0.2) < 1e-9


def test_snapshot_freshness_fields():
    now = time.time()
    accounts = [_vault(1.5, 1.5, now - 12), _vault(1.2, 1.2, now - 4000)]
    snap = _monitor(accounts).get_observability_snapshot()
    # most-recent check ~12s ago, oldest ~4000s ago
    assert 10 <= snap["seconds_since_last_cv_check"] <= 60
    assert 3990 <= snap["oldest_cv_check_age_seconds"] <= 4010


def test_snapshot_count_gated_when_nothing_checked():
    # Vaults exist but none has been checked yet -> freshness fields omitted
    accounts = [_vault(math.inf, math.inf, None), _vault(1.2, 1.2, None)]
    snap = _monitor(accounts).get_observability_snapshot()
    assert "seconds_since_last_cv_check" not in snap
    assert "oldest_cv_check_age_seconds" not in snap
    assert snap["monitored_cv_count"] == 2


def test_snapshot_empty_book():
    snap = _monitor([]).get_observability_snapshot()
    assert snap["monitored_cv_count"] == 0
    assert "seconds_since_last_cv_check" not in snap


def test_signer_balance_cached_and_skipped_when_none():
    m = _monitor([_vault(1.5, 1.5, time.time())])
    # Force an RPC failure with no prior cached value -> field omitted entirely
    m.w3 = SimpleNamespace(eth=SimpleNamespace(get_balance=MagicMock(side_effect=RuntimeError("rpc down"))))
    snap = m.get_observability_snapshot()
    assert "signer_balance_eth" not in snap


# ---------------- exporter.build_metric_data ----------------


def test_build_metric_data_maps_fields_and_chain_label():
    payload = {
        "chains": {
            "1": {
                "monitored_cv_count": 5,
                "seconds_since_last_cv_check": 15.0,
                "cvs_below_external_boundary": 0,
                "signer_balance_eth": 0.83,
                "latest_scanned_block": 123,
                "liquidation_failure_count": 0,
                "failed_init_count": 0,
            }
        }
    }
    md = mx.build_metric_data(payload)
    names = {m["MetricName"] for m in md}
    assert "MonitoredCVCount" in names
    assert "SecondsSinceLastCVCheck" in names
    assert "OldestCVCheckAgeSeconds" not in names  # absent in payload -> skipped
    assert all(m["Dimensions"] == [{"Name": "Chain", "Value": "mainnet"}] for m in md)
    by = {m["MetricName"]: m for m in md}
    assert by["SignerBalanceEth"]["Unit"] == "None"
    assert by["MonitoredCVCount"]["Value"] == 5.0


def test_build_metric_data_skips_error_chains():
    payload = {"chains": {"1": {"error": "starting"}}}
    assert mx.build_metric_data(payload) == []


def test_build_metric_data_unknown_chain_uses_raw_id():
    payload = {"chains": {"999": {"monitored_cv_count": 1}}}
    md = mx.build_metric_data(payload)
    assert md[0]["Dimensions"] == [{"Name": "Chain", "Value": "999"}]


# ---------------- exporter.emit / scrape_once ----------------


def test_emit_batches_put_metric_data(monkeypatch):
    monkeypatch.setattr(mx, "DRY_RUN", False)
    cw = MagicMock()
    md = [{"MetricName": f"M{i}", "Value": float(i), "Unit": "Count", "Dimensions": []} for i in range(45)]
    mx.emit(cw, md)
    # 45 entries / batch of 20 -> 3 calls (20, 20, 5)
    assert cw.put_metric_data.call_count == 3
    sizes = [len(c.kwargs["MetricData"]) for c in cw.put_metric_data.call_args_list]
    assert sizes == [20, 20, 5]


def test_scrape_once_emits(monkeypatch):
    monkeypatch.setattr(mx, "DRY_RUN", False)
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"chains": {"1": {"monitored_cv_count": 2}}}
    fake_resp.raise_for_status.return_value = None
    monkeypatch.setattr(mx.requests, "get", lambda *a, **k: fake_resp)
    cw = MagicMock()
    n = mx.scrape_once(cw)
    assert n == 1
    cw.put_metric_data.assert_called_once()


def test_scrape_once_returns_zero_on_http_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mx.requests, "get", boom)
    assert mx.scrape_once(MagicMock()) == 0
