"""DEV-554: liquidation-path latency.

Covers, all with mocked RPC / mocked HealthStatViewer (no network):
  - P1: the 1inch min-interval rate limiter only waits the time still owed.
  - P2/P6: a steady-state (healthy) vault tick issues <=2 eth_calls, and the
    new check-liquidation gate is decision-equivalent to the old always-check path.
  - P3: a vault restored from persisted metadata makes ZERO immutable eth_calls,
    and to_dict/from_dict round-trips the immutable metadata.
  - P5: ABI JSON is parsed once and cached per path.
  - rpc_metrics: the per-thread eth_call counter that backs the DEBUG per-tick log.
"""

import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dotenv import load_dotenv

from app.liquidation import contracts, rpc_metrics, swap_1inch
from app.liquidation.account_monitor import AccountMonitor
from app.liquidation.config_loader import load_chain_config
from app.liquidation.swap_1inch import _MinIntervalRateLimiter
from app.liquidation.vaults import euler_vault
from app.liquidation.vaults.base_vault import BaseCollateralVault
from app.liquidation.vaults.euler_vault import EulerCollateralVault

EOA = "0xA94D9d3b3f2A69559E89ea05B91940166382E23a"
ADDR = "0x97a2B0FA27A1865FFCB730738Ba07e4BBf700720"


@pytest.fixture()
def config():
    load_dotenv(dotenv_path=".env.example")
    return load_chain_config(1)


# --------------------------------------------------------------------------- #
# Counting fake contracts: every .call() bumps a shared counter.
# --------------------------------------------------------------------------- #


class _CountingCall:
    def __init__(self, name, returns, counter):
        self._name = name
        self._returns = returns
        self._counter = counter

    def call(self, *args, **kwargs):
        self._counter[0] += 1
        value = self._returns.get(self._name)
        return value() if callable(value) else value


class _CountingFunctions:
    def __init__(self, returns, counter):
        self._returns = returns
        self._counter = counter

    def __getattr__(self, name):
        returns, counter = self._returns, self._counter

        def builder(*args, **kwargs):
            return _CountingCall(name, returns, counter)

        return builder


class _CountingContract:
    def __init__(self, returns, counter):
        self.functions = _CountingFunctions(returns, counter)


class _MockVault(BaseCollateralVault):
    """A BaseCollateralVault whose on-chain reads route through counting fakes."""

    protocol = "mock"

    def __init__(
        self,
        config,
        counter,
        *,
        in_hf=None,
        ex_hf=None,
        in_hf_raw=None,
        ex_hf_raw=None,
        ext_liq=False,
        can_liq=False,
        max_release=0,
    ):
        # Bypass the parent __init__ (no real contract discovery). Health factors can
        # be given as floats (in_hf/ex_hf) or as exact raw 1e18-scaled ints
        # (in_hf_raw/ex_hf_raw) to exercise the boundary precisely.
        self.config = config
        self.address = ADDR
        self.time_of_next_update = 0
        self.internal_health_score = math.inf
        self.external_health_score = math.inf
        # Init the raw attrs to the same sentinel as production; get_health_score
        # overwrites them from the health() mock during the tick.
        self.internal_health_score_raw = math.inf
        self.external_health_score_raw = math.inf
        self.internal_value_borrowed = 0
        self.external_value_borrowed = 0
        self.underlying_asset_address = None
        self.target_asset = "0x" + "11" * 20
        self.is_correlated = False
        self.last_checked_at = None
        self.collateral_decimals, self.collateral_symbol = 18, "eWETH"
        self.debt_decimals, self.debt_symbol = 6, "USDC"
        self._counter = counter
        in_raw = in_hf_raw if in_hf_raw is not None else int(in_hf * 1e18)
        ex_raw = ex_hf_raw if ex_hf_raw is not None else int(ex_hf * 1e18)
        self.health_state_viewer = _CountingContract({"health": (ex_raw, in_raw, 1000, 1000)}, counter)
        self.instance = _CountingContract(
            {
                "isExternallyLiquidated": ext_liq,
                "canLiquidate": can_liq,
                "maxRelease": max_release,
                "maxRepay": 0,
                "totalAssetsDepositedOrReserved": 0,
            },
            counter,
        )

    def _init_protocol_contracts(self, config):
        pass

    def get_collateral_for_borrower(self):
        return 0

    def simulate_liquidation(self, *args, **kwargs):
        return (False, None, None)


def _monitor(config):
    return AccountMonitor(chain_id=1, config=config, notify=False, execute_liquidation=False)


# --------------------------------------------------------------------------- #
# P1 — 1inch min-interval rate limiter
# --------------------------------------------------------------------------- #


def test_rate_limiter_no_wait_when_interval_elapsed(monkeypatch):
    clock = {"t": 100.0}
    slept = []
    monkeypatch.setattr(swap_1inch.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(swap_1inch.time, "sleep", lambda s: slept.append(s))

    limiter = _MinIntervalRateLimiter(1.1)
    assert limiter.acquire() == 0.0  # first call: nothing owed
    clock["t"] += 5.0  # idle well past the interval
    assert limiter.acquire() == 0.0  # still nothing owed
    assert slept == []  # never actually slept on an idle limiter


def test_rate_limiter_waits_only_residual(monkeypatch):
    clock = {"t": 0.0}
    slept = []
    monkeypatch.setattr(swap_1inch.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(swap_1inch.time, "sleep", lambda s: slept.append(s))

    limiter = _MinIntervalRateLimiter(1.1)
    assert limiter.acquire() == 0.0  # first request now
    clock["t"] = 0.4  # a second request arrives 0.4s later
    waited = limiter.acquire()
    assert waited == pytest.approx(0.7)  # only the residual 1.1 - 0.4, not a flat 1.1
    assert slept == [pytest.approx(0.7)]


def test_swapper_uses_shared_limiter_not_flat_sleep():
    # The module no longer flat-sleeps; it goes through the shared limiter.
    import inspect

    src = inspect.getsource(swap_1inch)
    assert "time.sleep(1.1)" not in src
    assert src.count("_ONEINCH_RATE_LIMITER.acquire()") == 4  # quote, swap, allowance, approve


# --------------------------------------------------------------------------- #
# P2/P6 — steady-state tick eth_call count + decision-equivalence
# --------------------------------------------------------------------------- #


def test_steady_state_tick_issues_two_eth_calls(config):
    """A healthy, non-externally-liquidated vault tick: health() + isExternallyLiquidated()
    = 2 eth_calls, and the 5-call check_liquidation is skipped entirely."""
    monitor = _monitor(config)
    counter = [0]
    account = _MockVault(config, counter, in_hf=2.0, ex_hf=2.0, ext_liq=False)
    monitor.accounts[ADDR] = account
    handled = MagicMock()
    monitor._handle_liquidation = handled

    monitor.update_account_liquidity(ADDR)

    assert counter[0] == 2, f"steady-state tick must issue exactly 2 eth_calls, got {counter[0]}"
    handled.assert_not_called()


@pytest.mark.parametrize(
    "in_hf,ex_hf,ext_liq,can_liq,max_release,expect_handle,expect_calls",
    [
        # healthy: gate closed, no check_liquidation, no liquidation handling.
        (2.0, 2.0, False, False, 0, False, 2),
        # internally unhealthy (canLiquidate consistent with inHF<1): gate opens, handle.
        (0.5, 2.0, False, True, 0, True, 7),
        # externally unhealthy (canLiquidate consistent with exHF<1): gate opens, handle.
        (2.0, 0.5, False, True, 0, True, 7),
        # externally liquidated WITH reserved credit to release: gate opens, handle.
        (2.0, 2.0, True, False, 10, True, 7),
        # externally liquidated but nothing to release and healthy HFs: gate opens
        # (ext_liq), check runs, but the condition is False so NO handling — exactly
        # the old behaviour.
        (2.0, 2.0, True, False, 0, False, 7),
    ],
)
def test_gate_is_decision_equivalent(config, in_hf, ex_hf, ext_liq, can_liq, max_release, expect_handle, expect_calls):
    """The check-liquidation gate must reproduce the old always-check decision for
    every input where canLiquidate() is consistent with the viewer HFs (the relied-on
    invariant), while only paying for check_liquidation when the gate opens."""
    monitor = _monitor(config)
    counter = [0]
    account = _MockVault(
        config, counter, in_hf=in_hf, ex_hf=ex_hf, ext_liq=ext_liq, can_liq=can_liq, max_release=max_release
    )
    monitor.accounts[ADDR] = account
    handled = MagicMock()
    monitor._handle_liquidation = handled

    monitor.update_account_liquidity(ADDR)

    assert handled.called is expect_handle
    assert counter[0] == expect_calls


def test_gate_opens_on_raw_hf_just_below_one_despite_float_rounding(config):
    """Option A precision fix: raw inHF = 1e18 - 1 is truly liquidatable, but /1e18
    rounds to exactly 1.0 in float64 (ulp ~128 at 1e18). The old float gate
    (internal_health_score < 1) would WRONGLY skip check_liquidation; the raw-integer
    gate must OPEN it."""
    monitor = _monitor(config)
    counter = [0]
    account = _MockVault(config, counter, in_hf_raw=10**18 - 1, ex_hf_raw=2 * 10**18, can_liq=True)
    monitor.accounts[ADDR] = account
    handled = MagicMock()
    monitor._handle_liquidation = handled

    monitor.update_account_liquidity(ADDR)

    # The /1e18 float rounded a just-below-1 HF up to exactly 1.0 — the precision loss.
    assert account.internal_health_score == 1.0
    # ...but the raw integer is below the boundary, so the gate opened, the
    # authoritative on-chain check ran (5 calls), and a liquidation was handled.
    assert account.internal_health_score_raw == 10**18 - 1
    assert counter[0] == 7  # 2 (update_liquidity) + 5 (check_liquidation)
    handled.assert_called_once()


def test_clearly_healthy_vault_skips_check_with_raw_gate(config):
    """A clearly-healthy vault (raw HF = 2e18) still SKIPS check_liquidation (2 calls)."""
    monitor = _monitor(config)
    counter = [0]
    account = _MockVault(config, counter, in_hf_raw=2 * 10**18, ex_hf_raw=2 * 10**18)
    monitor.accounts[ADDR] = account
    handled = MagicMock()
    monitor._handle_liquidation = handled

    monitor.update_account_liquidity(ADDR)

    assert account.internal_health_score_raw == 2 * 10**18
    assert counter[0] == 2
    handled.assert_not_called()


# --------------------------------------------------------------------------- #
# P3 — restore from persisted metadata makes no immutable RPC
# --------------------------------------------------------------------------- #


def _euler_metadata():
    return {
        "balanceOf": 123456,
        "collateral_decimals": 18,
        "collateral_symbol": "eWETH",
        "debt_decimals": 6,
        "debt_symbol": "USDC",
        "underlying_asset_symbol": "WETH",
        "asset_address": "0x" + "a1" * 20,
        "underlying_asset_address": "0x" + "a2" * 20,
        "target_asset": "0x" + "a3" * 20,
        "target_vault_address": "0x" + "a4" * 20,
        "intermediate_vault_address": "0x" + "a5" * 20,
        "unit_of_account": "0x" + "a6" * 20,
        "vault_manager_address": "0x" + "a7" * 20,
        "oracle_router_address": "0x" + "a8" * 20,
        "vault_name": "Twyne CV",
        "vault_symbol": "tCV",
    }


def test_euler_restore_from_metadata_makes_no_immutable_rpc(config, monkeypatch):
    counter = [0]

    def fake_cci(address, abi_path, cfg):
        # Returning a counting contract proves no .call() is issued during restore.
        return _CountingContract({}, counter)

    monkeypatch.setattr(euler_vault, "create_contract_instance", fake_cci)

    meta = _euler_metadata()
    data = {
        "address": ADDR,
        "protocol": "euler",
        "time_of_next_update": 1.0,
        "internal_health_score": 1.5,
        "external_health_score": 1.5,
        "metadata": meta,
    }

    vault = EulerCollateralVault.from_dict(data, config)

    assert counter[0] == 0, "restore from metadata must issue ZERO immutable eth_calls"
    # Immutable attributes came straight from the persisted metadata.
    assert vault.balanceOf == 123456
    assert vault.asset_address == meta["asset_address"]
    assert vault.target_asset == meta["target_asset"]
    assert vault.unit_of_account == meta["unit_of_account"]
    assert vault.oracle_router_address == meta["oracle_router_address"]
    # Cached token metadata (P6) restored, not re-fetched.
    assert (vault.collateral_decimals, vault.collateral_symbol) == (18, "eWETH")
    assert (vault.debt_decimals, vault.debt_symbol) == (6, "USDC")


def test_metadata_round_trips_through_to_dict(config, monkeypatch):
    """A restored vault's to_dict must re-emit exactly the immutable metadata it was
    built from, so a live-discovered vault persists everything a restore needs."""
    monkeypatch.setattr(euler_vault, "create_contract_instance", lambda a, p, c: _CountingContract({}, [0]))
    meta = _euler_metadata()
    data = {
        "address": ADDR,
        "protocol": "euler",
        "time_of_next_update": 1.0,
        "internal_health_score": 1.5,
        "external_health_score": 1.5,
        "metadata": meta,
    }

    vault = EulerCollateralVault.from_dict(data, config)
    out = vault.to_dict()["metadata"]

    for key, value in meta.items():
        assert out[key] == value, f"metadata key {key} did not round-trip"


def test_multi_vault_restore_has_no_per_vault_metadata_rpc(config, tmp_path, monkeypatch):
    """A multi-vault book reloads with ZERO immutable metadata eth_calls (the health
    refresh is the only RPC and is stubbed here to isolate reconstruction)."""
    counter = [0]
    monkeypatch.setattr(euler_vault, "create_contract_instance", lambda a, p, c: _CountingContract({}, counter))
    # Isolate reconstruction from the (separate, parallelized) health refresh.
    monkeypatch.setattr(EulerCollateralVault, "update_liquidity", lambda self: [1.5, 1.5, False])

    state = {
        "version": 1,
        "last_saved_block": 4242,
        "accounts": {},
        "failed_initializations": {},
    }
    for i in range(3):
        addr = f"0x{i:040x}"
        state["accounts"][addr] = {
            "address": addr,
            "protocol": "euler",
            "time_of_next_update": 1.0 + i,
            "internal_health_score": 1.5,
            "external_health_score": 1.5,
            "metadata": _euler_metadata(),
        }

    path = str(tmp_path / "mainnet_state.json")
    import json

    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)

    monitor = _monitor(config)
    monitor.load_state(path)

    assert counter[0] == 0, "restore of a 3-vault book must issue zero immutable metadata eth_calls"
    assert len(monitor.accounts) == 3
    assert monitor.update_queue.qsize() == 3
    assert monitor.last_saved_block == 4242


# --------------------------------------------------------------------------- #
# P6 — get_position_stats uses cached metadata (one eth_call only)
# --------------------------------------------------------------------------- #


def test_get_position_stats_issues_single_eth_call(config):
    counter = [0]
    raw = (
        1_500_000_000_000_000_000,
        3_000_000_000_000_000_000_000,
        500_000_000_000_000_000,
        1_000_000_000_000_000_000_000,
        900_000_000,
        900_000_000_000_000_000_000,
        8500,
        3000,
        8000,
        2250,
        2_000_000_000_000_000_000,
        2_833_333_000_000_000_000,
        9000,
    )
    account = _MockVault(config, counter, in_hf=2.0, ex_hf=2.0, ext_liq=False)
    account.health_state_viewer = _CountingContract({"positionStats": raw}, counter)

    stats = account.get_position_stats()

    assert counter[0] == 1, "get_position_stats must issue exactly one eth_call (no token-metadata RPC)"
    assert stats is not None
    # Decimals/symbol came from the cached metadata, not RPC.
    assert stats.collateral_symbol == "eWETH"
    assert stats.debt_symbol == "USDC"
    assert stats.borrow_native == 900.0  # 900_000_000 / 1e6 (cached debt decimals)


# --------------------------------------------------------------------------- #
# P5 — ABI parse cache
# --------------------------------------------------------------------------- #


def test_create_contract_instance_caches_abi():
    contracts._load_abi.cache_clear()
    captured = {}
    cfg = SimpleNamespace(
        w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda address, abi: captured.setdefault("abi", abi)))
    )
    path = "contracts/IERC20.json"

    contracts.create_contract_instance("0x" + "00" * 20, path, cfg)
    contracts.create_contract_instance("0x" + "11" * 20, path, cfg)

    info = contracts._load_abi.cache_info()
    assert info.misses == 1, "ABI file must be parsed exactly once"
    assert info.hits >= 1, "second call must hit the cache"


# --------------------------------------------------------------------------- #
# rpc_metrics — per-thread eth_call counter backing the DEBUG per-tick log
# --------------------------------------------------------------------------- #


def test_rpc_metrics_counts_only_eth_call():
    rpc_metrics.reset_eth_call_count()

    class _Provider:
        def make_request(self, method, params):
            return {"result": "0x"}

    w3 = SimpleNamespace(provider=_Provider())
    assert rpc_metrics.install_eth_call_counter(w3) is True
    # Idempotent: a second install is a no-op.
    assert rpc_metrics.install_eth_call_counter(w3) is False

    w3.provider.make_request("eth_call", [])
    w3.provider.make_request("eth_getTransactionCount", [])
    w3.provider.make_request("eth_call", [])

    assert rpc_metrics.get_eth_call_count() == 2  # only eth_call is counted


def test_rpc_metrics_install_is_fail_open_on_bad_w3():
    # No provider attribute → returns False, never raises.
    assert rpc_metrics.install_eth_call_counter(SimpleNamespace()) is False
