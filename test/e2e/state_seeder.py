"""Python port of test/LiquidationStateBuilder.sol against a live anvil fork (DEV-579).

Manufactures liquidatable Twyne Collateral Vaults on a mainnet fork by interacting
with the ALREADY-DEPLOYED protocol, using anvil RPC cheatcodes as the live-anvil
equivalents of the forge cheatcodes (see anvil.py). Covers all six variants:
Euler/Aave x internal / external-with-debt / external-zero-debt.

Internal variants use a FAITHFUL small price drop (DEV-579 decision): the smallest
drop that flips canLiquidate(), so the bot's oracle-derived gross profit stays
positive and the real profitability gate is exercised — not a 95% crash that the bot
would (correctly) treat as unprofitable.
"""

from __future__ import annotations

from dataclasses import dataclass

from web3 import Web3

from .anvil import AnvilFork, encode_call
from .artifacts import creation_bytecode, deployed_bytecode

# ---- live mainnet addresses (chain 1) — mirror LiquidationStateBuilder.sol --------
FACTORY = "0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332"
TWYNE_EVC = "0xef39D6493884C4C84D38a4bFF879Ce16CEdE702a"
EULER_ORACLE_ROUTER = "0xb001f039D76bA48E577A17c04b6940DB37aF8648"

IV_EULER_EWETH = "0x87b8081A3ace680f35125F469526Ac10f5418Ca7"
EULER_WETH = "0xD8b27CF359b7D15710a5BE299AF6e7Bf904984C2"
EULER_USDC = "0x797DD80692c3b2dAdabCe8e30C07fDE5307D48a9"

AAVE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
IV_AAVE_AWSTETH = "0x75029a47f28550C93Ad5A3BbD2d9b5315204B561"
AWSTETH_WRAPPER = "0xFaBA8f777996C0C28fe9e6554D84cB30ca3e1881"
WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USD = "0x0000000000000000000000000000000000000348"  # address(840)
ZERO = "0x0000000000000000000000000000000000000000"

VAULT_TYPE_EULER = 0
VAULT_TYPE_AAVE = 1
MAX_UINT = 2**256 - 1

# ---- minimal ABI fragments (reads) ----------------------------------------------
_CV_VIEW = [
    {"inputs": [], "name": n, "outputs": [{"type": t}], "stateMutability": "view", "type": "function"}
    for n, t in [
        ("canLiquidate", "bool"),
        ("isExternallyLiquidated", "bool"),
        ("maxRepay", "uint256"),
        ("maxRelease", "uint256"),
    ]
]
_EVAULT_VIEW = [
    {
        "inputs": [],
        "name": "totalAssets",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "caps",
        "outputs": [{"type": "uint16"}, {"type": "uint16"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "governorAdmin",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {"inputs": [], "name": "EVC", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [{"type": "address"}],
        "name": "balanceOf",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"type": "address"}],
        "name": "debtOf",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"type": "address"}],
        "name": "LTVFull",
        "outputs": [{"type": "uint16"}, {"type": "uint16"}, {"type": "uint16"}, {"type": "uint48"}, {"type": "uint32"}],
        "stateMutability": "view",
        "type": "function",
    },
]
_ROUTER_VIEW = [
    {"inputs": [], "name": "governor", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [{"type": "uint256"}, {"type": "address"}, {"type": "address"}],
        "name": "getQuote",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]
_AAVE_POOL_VIEW = [
    {
        "inputs": [],
        "name": "ADDRESSES_PROVIDER",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]
_ADDR_PROVIDER_VIEW = [
    {
        "inputs": [],
        "name": "getPriceOracle",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]
_AAVE_ORACLE_VIEW = [
    {
        "inputs": [{"type": "address"}],
        "name": "getSourceOfAsset",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]
_FEED_VIEW = [
    {
        "inputs": [],
        "name": "latestAnswer",
        "outputs": [{"type": "int256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class CVHandles:
    cv: str
    intermediate_vault: str
    collateral_asset: str
    collateral_underlying: str
    target_vault: str
    target_asset: str


def _addr(label: str) -> str:
    """Deterministic throwaway EOA from a label (cf. forge makeAddr)."""
    return Web3.to_checksum_address("0x" + Web3.keccak(text=label).hex()[-40:])


class StateSeeder:
    def __init__(self, fork: AnvilFork):
        self.fork = fork
        self.w3 = fork.w3

    # -- small helpers -----------------------------------------------------
    def _c(self, addr, frag):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=frag)

    def _approve(self, owner, token, spender, amount=MAX_UINT):
        self.fork.send(
            owner,
            token,
            encode_call(
                "approve(address,uint256)", ["address", "uint256"], [Web3.to_checksum_address(spender), amount]
            ),
        )

    def _deposit(self, caller, vault, amount, to):
        self.fork.send(
            caller,
            vault,
            encode_call("deposit(uint256,address)", ["uint256", "address"], [amount, Web3.to_checksum_address(to)]),
        )

    def _cv_from_receipt(self, rcpt) -> str:
        topic0 = Web3.keccak(text="T_CollateralVaultCreated(address)")
        for log in rcpt["logs"]:
            if (
                Web3.to_checksum_address(log["address"]) == Web3.to_checksum_address(FACTORY)
                and log["topics"][0] == topic0
            ):
                return Web3.to_checksum_address("0x" + log["topics"][1].hex()[-40:])
        raise RuntimeError("T_CollateralVaultCreated not found in receipt")

    def _supply_cap_headroom(self, vault) -> int:
        caps = self._c(vault, _EVAULT_VIEW).functions.caps().call()
        supply_cap_raw = caps[0]
        if supply_cap_raw == 0:
            return 50 * 10**18
        cap = (10 ** (supply_cap_raw & 63)) * (supply_cap_raw >> 6) // 100
        used = self._c(vault, _EVAULT_VIEW).functions.totalAssets().call()
        return cap - used if cap > used else 0

    # -- CV view shortcuts -------------------------------------------------
    def can_liquidate(self, cv) -> bool:
        return self._c(cv, _CV_VIEW).functions.canLiquidate().call()

    def is_externally_liquidated(self, cv) -> bool:
        return self._c(cv, _CV_VIEW).functions.isExternallyLiquidated().call()

    def max_repay(self, cv) -> int:
        return self._c(cv, _CV_VIEW).functions.maxRepay().call()

    def max_release(self, cv) -> int:
        return self._c(cv, _CV_VIEW).functions.maxRelease().call()

    # -- IV top-ups (cap-aware) -------------------------------------------
    def _top_up_iv(self, iv, share_token, underlying, clp_label):
        headroom = self._supply_cap_headroom(iv)
        if headroom == 0:
            return
        target_shares = headroom * 8 // 10
        clp = _addr(clp_label)
        underlying_to_wrap = target_shares * 12 // 10
        self.fork.deal(underlying, clp, underlying_to_wrap)
        self._approve(clp, underlying, share_token)
        self._deposit(clp, share_token, underlying_to_wrap, clp)  # wrap -> shares
        shares = self._c(share_token, _EVAULT_VIEW).functions.balanceOf(clp).call()
        to_deposit = min(shares, target_shares)
        self._approve(clp, share_token, iv)
        self._deposit(clp, iv, to_deposit, clp)

    # ===================== Euler builders ================================
    def create_euler_cv(self, borrower, liq_ltv, collateral_underlying, borrow_amount) -> CVHandles:
        self._top_up_iv(IV_EULER_EWETH, EULER_WETH, WETH, "twyneCLP")
        data = encode_call(
            "createCollateralVault(uint8,address,address,uint256,address)",
            ["uint8", "address", "address", "uint256", "address"],
            [VAULT_TYPE_EULER, IV_EULER_EWETH, EULER_USDC, liq_ltv, ZERO],
        )
        rcpt = self.fork.send(borrower, FACTORY, data)
        cv = self._cv_from_receipt(rcpt)

        self.fork.deal(WETH, borrower, collateral_underlying)
        self._approve(borrower, WETH, cv)
        items = [
            (
                cv,
                borrower,
                0,
                bytes.fromhex(encode_call("depositUnderlying(uint256)", ["uint256"], [collateral_underlying])[2:]),
            ),
            (
                cv,
                borrower,
                0,
                bytes.fromhex(
                    encode_call("borrow(uint256,address)", ["uint256", "address"], [borrow_amount, borrower])[2:]
                ),
            ),
        ]
        batch = encode_call("batch((address,address,uint256,bytes)[])", ["(address,address,uint256,bytes)[]"], [items])
        self.fork.send(borrower, TWYNE_EVC, batch)
        return CVHandles(cv, IV_EULER_EWETH, EULER_WETH, WETH, EULER_USDC, USDC)

    def make_euler_internally_liquidatable(self, h: CVHandles):
        """Drop only Twyne's oracle-router collateral price by the SMALLEST amount that
        flips canLiquidate() (faithful: keeps the bot's gross profit positive)."""
        router = self._c(EULER_ORACLE_ROUTER, _ROUTER_VIEW)
        p_share = router.functions.getQuote(10**18, Web3.to_checksum_address(h.collateral_asset), USD).call()
        assert p_share > 0, "could not read live collateral price"

        mock = self.fork.deploy("0x000000000000000000000000000000000000c0DE", _creation_hex_mock("MockPriceOracle"))
        governor = router.functions.governor().call()

        def set_price(numer, denom):
            self.fork.send(
                "0x000000000000000000000000000000000000c0DE",
                mock,
                encode_call(
                    "setPrice(address,address,uint256)",
                    ["address", "address", "uint256"],
                    [Web3.to_checksum_address(h.collateral_asset), USD, p_share * numer // denom],
                ),
            )

        # Point the router at the mock (start at full price so canLiquidate is unchanged).
        set_price(1, 1)
        self.fork.send(
            governor,
            EULER_ORACLE_ROUTER,
            encode_call(
                "govSetConfig(address,address,address)",
                ["address", "address", "address"],
                [Web3.to_checksum_address(h.collateral_asset), USD, mock],
            ),
        )

        # Binary-search the largest price fraction (smallest drop) that still liquidates.
        lo, hi = 0, 1000  # price = p_share * k/1000
        # invariant: canLiquidate at lo (0 price), not at hi (full). find threshold.
        if self.can_liquidate(h.cv):
            frac = 1000  # already liquidatable at full price (shouldn't happen for a healthy CV)
        else:
            for _ in range(20):
                mid = (lo + hi) // 2
                set_price(mid, 1000)
                if self.can_liquidate(h.cv):
                    lo = mid
                else:
                    hi = mid
            frac = lo
        set_price(frac, 1000)

        assert self.can_liquidate(h.cv), "CV not internally liquidatable"
        assert not self.is_externally_liquidated(h.cv), "CV unexpectedly externally liquidated"
        assert self.max_repay(h.cv) > 0, "maxRepay should be > 0"

    def make_euler_externally_liquidated_with_debt(self, h: CVHandles, ext_liquidator):
        ev = self._c(h.target_vault, _EVAULT_VIEW)
        orig = ev.functions.LTVFull(Web3.to_checksum_address(h.collateral_asset)).call()
        orig_borrow_ltv, orig_liq_ltv = orig[0], orig[1]

        self._lower_euler_external_ltv(h.target_vault, h.collateral_asset, 500, 1000)
        self.fork.warp(3600)
        debt = ev.functions.debtOf(Web3.to_checksum_address(h.cv)).call()
        self._euler_external_liquidate(h, ext_liquidator, debt // 4)
        # restore healthy external LTV
        self.fork.send(
            ev.functions.governorAdmin().call(),
            h.target_vault,
            encode_call(
                "setLTV(address,uint16,uint16,uint32)",
                ["address", "uint16", "uint16", "uint32"],
                [Web3.to_checksum_address(h.collateral_asset), orig_borrow_ltv, orig_liq_ltv, 0],
            ),
        )

        assert self.is_externally_liquidated(h.cv), "CV not externally liquidated"
        assert self.max_repay(h.cv) > 0, "expected residual debt"
        assert self.max_release(h.cv) > 0, "expected reserved credit"

    def make_euler_externally_liquidated_zero_debt(self, h: CVHandles, ext_liquidator):
        self._lower_euler_external_ltv(h.target_vault, h.collateral_asset, 500, 1000)
        self.fork.warp(3600)
        self._euler_external_liquidate(h, ext_liquidator, MAX_UINT)

        assert self.is_externally_liquidated(h.cv), "CV not externally liquidated"
        assert self.max_repay(h.cv) == 0, "expected zero debt"
        assert self.max_release(h.cv) > 0, "expected reserved credit"

    def _lower_euler_external_ltv(self, target_vault, collateral, borrow_ltv, liq_ltv):
        gov = self._c(target_vault, _EVAULT_VIEW).functions.governorAdmin().call()
        self.fork.send(
            gov,
            target_vault,
            encode_call(
                "setLTV(address,uint16,uint16,uint32)",
                ["address", "uint16", "uint16", "uint32"],
                [Web3.to_checksum_address(collateral), borrow_ltv, liq_ltv, 0],
            ),
        )

    def _euler_external_liquidate(self, h: CVHandles, liq, repay_assets):
        self._deal_etoken(h.collateral_asset, h.collateral_underlying, liq, 100 * 10**18)
        euler_evc = self._c(h.target_vault, _EVAULT_VIEW).functions.EVC().call()
        self.fork.send(
            liq,
            euler_evc,
            encode_call(
                "enableCollateral(address,address)",
                ["address", "address"],
                [liq, Web3.to_checksum_address(h.collateral_asset)],
            ),
        )
        self.fork.send(
            liq,
            euler_evc,
            encode_call(
                "enableController(address,address)",
                ["address", "address"],
                [liq, Web3.to_checksum_address(h.target_vault)],
            ),
        )
        self.fork.send(
            liq,
            h.target_vault,
            encode_call(
                "liquidate(address,address,uint256,uint256)",
                ["address", "address", "uint256", "uint256"],
                [Web3.to_checksum_address(h.cv), Web3.to_checksum_address(h.collateral_asset), repay_assets, 0],
            ),
        )

    def _deal_etoken(self, etoken, underlying, to, max_underlying):
        headroom = self._supply_cap_headroom(etoken)
        amt = max_underlying if max_underlying < headroom else headroom * 8 // 10
        self.fork.deal(underlying, to, amt)
        self._approve(to, underlying, etoken)
        self._deposit(to, etoken, amt, to)

    # ===================== Aave builders =================================
    def create_aave_cv(self, borrower, liq_ltv, collateral_underlying, borrow_amount) -> CVHandles:
        self._top_up_iv(IV_AAVE_AWSTETH, AWSTETH_WRAPPER, WSTETH, "twyneAaveCLP")
        data = encode_call(
            "createCollateralVault(uint8,address,address,uint256,address)",
            ["uint8", "address", "address", "uint256", "address"],
            [VAULT_TYPE_AAVE, IV_AAVE_AWSTETH, AAVE_POOL, liq_ltv, WETH],
        )
        rcpt = self.fork.send(borrower, FACTORY, data)
        cv = self._cv_from_receipt(rcpt)

        self.fork.deal(WSTETH, borrower, collateral_underlying)
        self._approve(borrower, WSTETH, cv)
        items = [
            (
                cv,
                borrower,
                0,
                bytes.fromhex(encode_call("depositUnderlying(uint256)", ["uint256"], [collateral_underlying])[2:]),
            ),
            (
                cv,
                borrower,
                0,
                bytes.fromhex(
                    encode_call("borrow(uint256,address)", ["uint256", "address"], [borrow_amount, borrower])[2:]
                ),
            ),
        ]
        batch = encode_call("batch((address,address,uint256,bytes)[])", ["(address,address,uint256,bytes)[]"], [items])
        self.fork.send(borrower, TWYNE_EVC, batch)
        return CVHandles(cv, IV_AAVE_AWSTETH, AWSTETH_WRAPPER, WSTETH, AAVE_POOL, WETH)

    def make_aave_internally_liquidatable(self, h: CVHandles):
        feed, orig = self._drop_aave_feed(h.collateral_underlying, 100)  # start at full price
        # binary-search smallest drop that flips canLiquidate
        lo, hi = 0, 100
        if not self.can_liquidate(h.cv):
            for _ in range(18):
                mid = (lo + hi) // 2
                self._set_aave_feed(feed, orig * mid // 100)
                if self.can_liquidate(h.cv):
                    lo = mid
                else:
                    hi = mid
            self._set_aave_feed(feed, orig * lo // 100)
        assert self.can_liquidate(h.cv), "Aave CV not internally liquidatable"
        assert not self.is_externally_liquidated(h.cv), "Aave CV unexpectedly externally liquidated"
        assert self.max_repay(h.cv) > 0, "maxRepay should be > 0"

    def make_aave_externally_liquidated_with_debt(self, h: CVHandles, ext_liquidator):
        feed, orig = self._drop_aave_feed(h.collateral_underlying, 35)
        self.fork.warp(3600)
        debt = self.max_repay(h.cv)
        self._aave_external_liquidate(h, ext_liquidator, debt // 4)
        self._set_aave_feed(feed, orig)
        assert self.is_externally_liquidated(h.cv), "Aave CV not externally liquidated"
        assert self.max_repay(h.cv) > 0, "expected residual debt"
        assert self.max_release(h.cv) > 0, "expected reserved credit"

    def make_aave_externally_liquidated_zero_debt(self, h: CVHandles, ext_liquidator):
        feed, orig = self._drop_aave_feed(h.collateral_underlying, 35)
        self.fork.warp(3600)
        debt = self.max_repay(h.cv)
        self._aave_external_liquidate(h, ext_liquidator, debt // 4)
        self._set_aave_feed(feed, orig)
        self._aave_repay_debt(h)
        assert self.is_externally_liquidated(h.cv), "Aave CV not externally liquidated"
        assert self.max_repay(h.cv) == 0, "expected zero debt"
        assert self.max_release(h.cv) > 0, "expected reserved credit"

    def _drop_aave_feed(self, underlying, pct):
        oracle = (
            self._c(self._c(AAVE_POOL, _AAVE_POOL_VIEW).functions.ADDRESSES_PROVIDER().call(), _ADDR_PROVIDER_VIEW)
            .functions.getPriceOracle()
            .call()
        )
        feed = (
            self._c(oracle, _AAVE_ORACLE_VIEW).functions.getSourceOfAsset(Web3.to_checksum_address(underlying)).call()
        )
        orig = self._c(feed, _FEED_VIEW).functions.latestAnswer().call()
        assert orig > 0, "could not read live aave feed"
        self.fork.set_code(feed, deployed_bytecode("MockAaveFeed"))
        self._set_aave_feed(feed, orig * pct // 100)
        return feed, orig

    def _set_aave_feed(self, feed, answer):
        self.fork.send(
            "0x000000000000000000000000000000000000c0DE",
            feed,
            encode_call("setPrice(uint256)", ["uint256"], [int(answer)]),
        )

    def _aave_repay_debt(self, h: CVHandles):
        repayer = _addr("twyneAaveRepayer")
        amount = self.max_repay(h.cv) + 10**18
        self.fork.deal(h.target_asset, repayer, amount)
        self._approve(repayer, h.target_asset, AAVE_POOL)
        self.fork.send(
            repayer,
            AAVE_POOL,
            encode_call(
                "repay(address,uint256,uint256,address)",
                ["address", "uint256", "uint256", "address"],
                [Web3.to_checksum_address(h.target_asset), amount, 2, Web3.to_checksum_address(h.cv)],
            ),
        )

    def _aave_external_liquidate(self, h: CVHandles, liq, debt_to_cover):
        self.fork.deal(h.target_asset, liq, 100 * 10**18)
        self._approve(liq, h.target_asset, AAVE_POOL)
        self.fork.send(
            liq,
            AAVE_POOL,
            encode_call(
                "liquidationCall(address,address,address,uint256,bool)",
                ["address", "address", "address", "uint256", "bool"],
                [
                    Web3.to_checksum_address(h.collateral_underlying),
                    Web3.to_checksum_address(h.target_asset),
                    Web3.to_checksum_address(h.cv),
                    debt_to_cover,
                    False,
                ],
            ),
        )


def _creation_hex_mock(name: str) -> str:
    bc = creation_bytecode(name)
    return bc if bc.startswith("0x") else "0x" + bc
