// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "forge-std/interfaces/IERC20.sol";
import {IEVC} from "contracts/IEVC.sol";
import {IEVault} from "contracts/IEVault.sol";
import {IEulerCollateralVault} from "contracts/IEulerCollateralVault.sol";
import {MockPriceOracle} from "contracts/MockPriceOracle.sol";
import {MockAaveFeed, IAggregator} from "./MockAaveFeed.sol";

/// @notice Minimal view of the deployed Twyne CollateralVaultFactory (1.0.7).
/// @dev 1.0.7 replaces the single `createCollateralVault(uint8,...)` with one function per
///      collateral vault type, and replaces the boolean `paused()` with a three-state
///      `pauseState()` (0 Active, 1 Frozen, 2 Paused).
interface ICollateralVaultFactory {
    function createEulerCollateralVault(address _intermediateVault, address _targetVault, uint256 _liqLTV)
        external
        returns (address);
    function createAaveV3CollateralVault(
        address _intermediateVault,
        address _targetVault,
        uint256 _liqLTV,
        address _targetAsset
    ) external returns (address);
    function isCollateralVault(address) external view returns (bool);
    function pauseState() external view returns (uint8);
    function EVC() external view returns (address);
}

/// @notice Twyne 1.0.7 collateral vault deposit surface, shared by the Euler and the Aave vault.
/// @dev `depositUnderlying` is gone. `deposit` pulls the RECEIPT token (eVault shares for an
///      Euler vault, aToken-wrapper shares for an Aave vault) from the borrower, so the borrower
///      wraps the underlying first. `skim` books receipt tokens that were sent to the vault.
interface ITwyneCollateralVault {
    function deposit(uint256 assets) external;
    function skim() external;
}

/// @notice Minimal view of an Euler price-oracle router (euler-price-oracle EulerRouter).
interface IEulerRouter {
    function governor() external view returns (address);
    function govSetConfig(address base, address quote, address oracle) external;
    function getConfiguredOracle(address base, address quote) external view returns (address);
    function getQuote(uint256 inAmount, address base, address quote) external view returns (uint256);
}

/// @notice Minimal view of the deployed Twyne VaultManager (1.0.7).
/// @dev 1.0.7 keys the liquidation parameters on (intermediateVault, targetAsset) and removes
///      `oracleRouter()`; the router is now the intermediate vault's `oracle()`.
interface IVaultManager {
    function liqParams(address intermediateVault, address targetAsset)
        external
        view
        returns (uint16 externalLiqBuffer, uint16 maxTwyneLiqLTV, uint16 borrowBuffer);
}

/// @notice Minimal Aave V3 Pool view for manufacturing/seizing external positions.
interface IAaveV3Pool {
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
    function repay(address asset, uint256 amount, uint256 interestRateMode, address onBehalfOf)
        external
        returns (uint256);
    function ADDRESSES_PROVIDER() external view returns (address);
}

interface IAaveAddressProvider {
    function getPriceOracle() external view returns (address);
}

interface IAaveOracle {
    function getSourceOfAsset(address asset) external view returns (address);
}

/// @notice Minimal Twyne Aave collateral vault view (live V3 ABI).
interface IAaveCV {
    function borrow(uint256 targetAmount, address receiver) external;
    function canLiquidate() external view returns (bool);
    function isExternallyLiquidated() external view returns (bool);
    function maxRepay() external view returns (uint256);
    function maxRelease() external view returns (uint256);
    function asset() external view returns (address);
    function targetAsset() external view returns (address);
    function underlyingAsset() external view returns (address);
}

/// @title LiquidationStateBuilder
/// @notice Foundry fixture that manufactures liquidatable Twyne Collateral Vaults (CVs) on a mainnet
///         fork by interacting with the ALREADY-DEPLOYED protocol (no from-scratch deploy, no
///         twyne-contracts source dependency). It creates a fresh CV against the live
///         CollateralVaultFactory + live intermediate vaults, then forces it into each of the six
///         liquidation variants (Euler + Aave x internal / external-with-debt / external-zero-debt)
///         using `vm.prank` / `vm.etch` against the live governors.
///
/// @dev    Block-independent by construction: the only fork requirement is a pinned recent block where
///         the V3 protocol + Aave integration are deployed (see foundry.toml fork_block_number). All
///         live addresses are sourced from tech-notes `TwyneAddresses_current_1.json` (DEV-569).
abstract contract LiquidationStateBuilder is Test {
    /* ---------------------------------------------------------------------- */
    /*                       Live mainnet addresses (chain 1)                  */
    /*        source: tech-notes/public-launch-addresses/TwyneAddresses_current_1.json          */
    /* ---------------------------------------------------------------------- */
    address internal constant FACTORY = 0xa1517cCe0bE75700A8838EA1cEE0dc383cd3A332;
    address internal constant TWYNE_EVC = 0xef39D6493884C4C84D38a4bFF879Ce16CEdE702a;
    address internal constant VAULT_MANAGER = 0x0acd3A3c8Ab6a5F7b5A594C88DFa28999dA858aC;
    address internal constant EULER_ORACLE_ROUTER = 0xb001f039D76bA48E577A17c04b6940DB37aF8648;

    // Euler integration
    address internal constant IV_EULER_EWETH = 0x87b8081A3ace680f35125F469526Ac10f5418Ca7; // intermediate vault
    address internal constant EULER_WETH = 0xD8b27CF359b7D15710a5BE299AF6e7Bf904984C2; // eulerWETH eToken (collateral)
    address internal constant EULER_USDC = 0x797DD80692c3b2dAdabCe8e30C07fDE5307D48a9; // eulerUSDC vault (target)

    // Aave integration (collateral wstETH via awstETH wrapper, borrow WETH)
    address internal constant AAVE_POOL = 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2;
    address internal constant AAVE_ORACLE_ROUTER = 0x5D7A67418ee94259fd3A6091E2Cc0baeedfFA185; // Twyne aave router
    address internal constant IV_AAVE_AWSTETH = 0x75029a47f28550C93Ad5A3BbD2d9b5315204B561; // intermediate vault
    address internal constant AWSTETH_WRAPPER = 0xFaBA8f777996C0C28fe9e6554D84cB30ca3e1881; // CV.asset()
    address internal constant WSTETH = 0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0; // wrapper underlying

    // Underlyings
    address internal constant WETH = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;
    address internal constant USDC = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;

    address internal constant USD = address(840); // unit of account


    /// @notice Pinned recent fork block where the V3 protocol + Aave integration are deployed and the
    ///         deployed ABI matches the interfaces below. See foundry.toml / docs for the archive-RPC note.
    /// @dev Public archive RPC used when FOUNDRY_ETH_RPC_URL is unset (e.g. local `forge test`).
    string internal constant DEFAULT_FORK_RPC = "https://eth.drpc.org";

    /// @notice Select a mainnet fork with FOUNDRY_ETH_RPC_URL (archive) or a public default.
    /// @dev Forks the LATEST block, because the fixture builds its state from live protocol
    ///      reads and Twyne 1.0.7 is newer than any pinned block. Set FOUNDRY_FORK_BLOCK to pin
    ///      a block. The fixture needs a chain on 1.0.7: point FOUNDRY_ETH_RPC_URL at an anvil
    ///      fork with Safe nonce 29 applied until the upgrade is live on mainnet.
    function _forkMainnet() internal {
        string memory rpc = vm.envOr("FOUNDRY_ETH_RPC_URL", DEFAULT_FORK_RPC);
        uint256 pinnedBlock = vm.envOr("FOUNDRY_FORK_BLOCK", uint256(0));
        if (pinnedBlock == 0) {
            vm.createSelectFork(rpc);
        } else {
            vm.createSelectFork(rpc, pinnedBlock);
        }

        (bool ok,) = VAULT_MANAGER.staticcall(
            abi.encodeWithSignature("liqParams(address,address)", address(0), address(0))
        );
        require(ok, "chain is not on Twyne 1.0.7 (VaultManager.liqParams missing): use an upgraded fork");
    }

    /// @notice Bundle of addresses describing a freshly-created CV under construction.
    struct CVHandles {
        address cv; // the collateral vault
        address intermediateVault; // IV the CV reserves credit from
        address collateralAsset; // CV.asset() — the eToken / aToken-wrapper share
        address collateralUnderlying; // underlying token deposited by the borrower (WETH / wstETH)
        address targetVault; // external protocol vault/pool the CV borrows from
        address targetAsset; // borrowed asset (USDC / WETH)
    }

    /* ====================================================================== */
    /*                              Euler builders                            */
    /* ====================================================================== */

    /// @notice Create a fresh Euler-backed CV against the live factory and open a borrow position.
    /// @param borrower account that owns the CV
    /// @param liqLTV Twyne liquidation LTV (1e4) — must be <= live liqParams(IV, targetAsset).maxTwyneLiqLTV
    /// @param collateralUnderlying amount of WETH the borrower deposits as collateral
    /// @param borrowAmount amount of USDC to borrow (must be within capacity)
    function _createEulerCV(address borrower, uint16 liqLTV, uint256 collateralUnderlying, uint256 borrowAmount)
        internal
        returns (CVHandles memory h)
    {
        h.intermediateVault = IV_EULER_EWETH;
        h.collateralAsset = EULER_WETH;
        h.collateralUnderlying = WETH;
        h.targetVault = EULER_USDC;
        h.targetAsset = USDC;

        // Ensure the IV has idle credit to reserve (top it up via a fresh CLP deposit, bounded by
        // the IV's supply cap headroom so we never trip E_SupplyCapExceeded).
        _topUpEulerIV(IV_EULER_EWETH, EULER_WETH, WETH);

        // Create the CV from the live factory (permissionless).
        vm.prank(borrower);
        h.cv = ICollateralVaultFactory(FACTORY).createEulerCollateralVault(IV_EULER_EWETH, EULER_USDC, liqLTV);
        vm.label(h.cv, "collateralVault");
        require(ICollateralVaultFactory(FACTORY).isCollateralVault(h.cv), "factory did not register CV");

        // Twyne 1.0.7: the CV pulls eulerWETH shares, so the borrower wraps the WETH first.
        deal(WETH, borrower, collateralUnderlying);
        vm.startPrank(borrower);
        IERC20(WETH).approve(h.collateralAsset, type(uint256).max);
        uint256 collateralShares = IEVault(h.collateralAsset).deposit(collateralUnderlying, borrower);

        // Deposit the collateral shares + borrow, in one EVC batch.
        IERC20(h.collateralAsset).approve(h.cv, type(uint256).max);
        IEVC.BatchItem[] memory items = new IEVC.BatchItem[](2);
        items[0] = IEVC.BatchItem({
            targetContract: h.cv,
            onBehalfOfAccount: borrower,
            value: 0,
            data: abi.encodeCall(ITwyneCollateralVault.deposit, (collateralShares))
        });
        items[1] = IEVC.BatchItem({
            targetContract: h.cv,
            onBehalfOfAccount: borrower,
            value: 0,
            data: abi.encodeCall(IEulerCollateralVault.borrow, (borrowAmount, borrower))
        });
        IEVC(TWYNE_EVC).batch(items);
        vm.stopPrank();
    }

    /// @notice Variant 1 — make an Euler CV Twyne-internally liquidatable (NOT externally liquidated).
    /// @dev Drops only Twyne's oracle-router collateral price (eulerWETH/USD). The external Euler router
    ///      is untouched, so the external position stays healthy and `isExternallyLiquidated()` is false.
    function makeEulerInternallyLiquidatable(CVHandles memory h) internal {
        IEulerRouter router = IEulerRouter(EULER_ORACLE_ROUTER);
        // live USD value of 1e18 eulerWETH shares, resolved through the router's current config
        uint256 pShare = router.getQuote(1e18, h.collateralAsset, USD);
        require(pShare > 0, "could not read live collateral price");

        MockPriceOracle mock = new MockPriceOracle();
        mock.setPrice(h.collateralAsset, USD, pShare / 20); // 95% collateral price drop

        vm.prank(router.governor());
        router.govSetConfig(h.collateralAsset, USD, address(mock));

        IEulerCollateralVault cv = IEulerCollateralVault(h.cv);
        require(cv.canLiquidate(), "CV not internally liquidatable");
        require(!cv.isExternallyLiquidated(), "CV unexpectedly externally liquidated");
        require(cv.maxRepay() > 0, "maxRepay should be > 0");
    }

    /// @notice Variant 2 — make an Euler CV externally liquidated while it still has target-asset debt.
    /// @dev Lowers the external Euler LTV of the collateral, warps past the cool-off, then a funded third
    ///      party PARTIALLY liquidates the CV on Euler, leaving residual USDC debt (maxRepay > 0).
    function makeEulerExternallyLiquidatedWithDebt(CVHandles memory h, address extLiquidator) internal {
        // remember the live external LTV so we can restore a healthy config afterwards
        (uint16 origBorrowLTV, uint16 origLiqLTV,,,) = IEVault(h.targetVault).LTVFull(h.collateralAsset);

        _lowerEulerExternalLTV(h.targetVault, h.collateralAsset, 0.05e4, 0.1e4);
        vm.warp(block.timestamp + 1 hours);

        uint256 debt = IEVault(h.targetVault).debtOf(h.cv);
        _eulerExternalLiquidate(h, extLiquidator, debt / 4); // partial seizure -> residual debt remains

        // Restore the original (healthy) external LTV so the residual position is no longer externally
        // liquidatable; the bot's WithDebt path requires a healthy external position with residual debt.
        vm.prank(IEVault(h.targetVault).governorAdmin());
        IEVault(h.targetVault).setLTV(h.collateralAsset, origBorrowLTV, origLiqLTV, 0);

        IEulerCollateralVault cv = IEulerCollateralVault(h.cv);
        require(cv.isExternallyLiquidated(), "CV not externally liquidated");
        require(cv.maxRepay() > 0, "expected residual debt (maxRepay > 0)");
        require(cv.maxRelease() > 0, "expected reserved credit (maxRelease > 0)");
    }

    /// @notice Variant 3 — make an Euler CV externally liquidated with all target debt cleared (zero debt),
    ///         on the normal-LTV path so reserved credit remains (maxRelease > 0).
    function makeEulerExternallyLiquidatedZeroDebt(CVHandles memory h, address extLiquidator) internal {
        _lowerEulerExternalLTV(h.targetVault, h.collateralAsset, 0.05e4, 0.1e4);
        vm.warp(block.timestamp + 1 hours);

        _eulerExternalLiquidate(h, extLiquidator, type(uint256).max); // full -> clears all debt

        IEulerCollateralVault cv = IEulerCollateralVault(h.cv);
        require(cv.isExternallyLiquidated(), "CV not externally liquidated");
        require(cv.maxRepay() == 0, "expected zero debt (maxRepay == 0)");
        require(cv.maxRelease() > 0, "expected reserved credit (maxRelease > 0)");
    }

    /* ====================================================================== */
    /*                               Aave builders                            */
    /* ====================================================================== */

    /// @notice Create a fresh Aave-backed CV (wstETH collateral, WETH debt) against the live factory.
    function _createAaveCV(address borrower, uint16 liqLTV, uint256 collateralUnderlying, uint256 borrowAmount)
        internal
        returns (CVHandles memory h)
    {
        h.intermediateVault = IV_AAVE_AWSTETH;
        h.collateralAsset = AWSTETH_WRAPPER;
        h.collateralUnderlying = WSTETH;
        h.targetVault = AAVE_POOL;
        h.targetAsset = WETH;

        _topUpAaveIV(IV_AAVE_AWSTETH, AWSTETH_WRAPPER, WSTETH);

        vm.prank(borrower);
        h.cv = ICollateralVaultFactory(FACTORY).createAaveV3CollateralVault(
            IV_AAVE_AWSTETH, AAVE_POOL, liqLTV, WETH
        );
        vm.label(h.cv, "aaveCollateralVault");
        require(ICollateralVaultFactory(FACTORY).isCollateralVault(h.cv), "factory did not register Aave CV");

        // Twyne 1.0.7: the CV pulls aToken-wrapper shares, so the borrower wraps the wstETH first.
        deal(WSTETH, borrower, collateralUnderlying);
        vm.startPrank(borrower);
        IERC20(WSTETH).approve(h.collateralAsset, type(uint256).max);
        uint256 collateralShares = IEVault(h.collateralAsset).deposit(collateralUnderlying, borrower);

        IERC20(h.collateralAsset).approve(h.cv, type(uint256).max);
        IEVC.BatchItem[] memory items = new IEVC.BatchItem[](2);
        items[0] = IEVC.BatchItem({
            targetContract: h.cv,
            onBehalfOfAccount: borrower,
            value: 0,
            data: abi.encodeCall(ITwyneCollateralVault.deposit, (collateralShares))
        });
        items[1] = IEVC.BatchItem({
            targetContract: h.cv,
            onBehalfOfAccount: borrower,
            value: 0,
            data: abi.encodeCall(IAaveCV.borrow, (borrowAmount, borrower))
        });
        IEVC(TWYNE_EVC).batch(items);
        vm.stopPrank();
    }

    /// @notice Variant 4 — make an Aave CV Twyne-internally liquidatable (NOT externally liquidated).
    /// @dev The Aave CV values collateral via the wrapper's `latestAnswer()` (Chainlink-derived), so drop
    ///      the underlying Chainlink feed. This trips the Twyne-LTV branch of `_canLiquidate` while the
    ///      aToken balance is untouched, so `isExternallyLiquidated()` stays false.
    function makeAaveInternallyLiquidatable(CVHandles memory h) internal {
        _dropAaveFeed(h.collateralUnderlying, 5); // 5% of live price -> 95% drop

        IAaveCV cv = IAaveCV(h.cv);
        require(cv.canLiquidate(), "Aave CV not internally liquidatable");
        require(!cv.isExternallyLiquidated(), "Aave CV unexpectedly externally liquidated");
        require(cv.maxRepay() > 0, "maxRepay should be > 0");
    }

    /// @notice Variant 5 — Aave CV externally liquidated with residual target debt (maxRepay > 0).
    /// @dev Drops the Aave Chainlink collateral feed, a funded third party PARTIALLY repays the debt on
    ///      Aave (seizing collateral), then the feed is restored so the residual position is healthy.
    function makeAaveExternallyLiquidatedWithDebt(CVHandles memory h, address extLiquidator) internal {
        (address feed, uint256 origAnswer) = _dropAaveFeed(h.collateralUnderlying, 35); // 35% of live -> 65% drop
        vm.warp(block.timestamp + 1 hours);

        uint256 debt = IAaveCV(h.cv).maxRepay();
        _aaveExternalLiquidate(h, extLiquidator, debt / 4); // small partial repay (dust-safe) -> residual debt

        _setAaveFeed(feed, origAnswer); // restore -> residual position healthy

        IAaveCV cv = IAaveCV(h.cv);
        require(cv.isExternallyLiquidated(), "Aave CV not externally liquidated");
        require(cv.maxRepay() > 0, "expected residual debt (maxRepay > 0)");
        require(cv.maxRelease() > 0, "expected reserved credit (maxRelease > 0)");
    }

    /// @notice Variant 6 — Aave CV externally liquidated with all target debt cleared (zero debt),
    ///         normal-LTV path so reserved credit remains (maxRelease > 0).
    /// @dev A price-drop full liquidation would seize all collateral, so instead: partially seize (sets the
    ///      external-liquidation flag, leaves collateral), restore the feed, then directly repay the
    ///      remaining Aave debt to zero. End state: externally liquidated, no debt, reserved credit intact.
    function makeAaveExternallyLiquidatedZeroDebt(CVHandles memory h, address extLiquidator) internal {
        (address feed, uint256 origAnswer) = _dropAaveFeed(h.collateralUnderlying, 35);
        vm.warp(block.timestamp + 1 hours);

        uint256 debt = IAaveCV(h.cv).maxRepay();
        _aaveExternalLiquidate(h, extLiquidator, debt / 4); // partial seizure -> sets flag, leaves collateral

        _setAaveFeed(feed, origAnswer); // restore healthy prices
        _aaveRepayDebt(h); // repay the remaining Aave debt to zero

        IAaveCV cv = IAaveCV(h.cv);
        require(cv.isExternallyLiquidated(), "Aave CV not externally liquidated");
        require(cv.maxRepay() == 0, "expected zero debt (maxRepay == 0)");
        require(cv.maxRelease() > 0, "expected reserved credit (maxRelease > 0)");
    }

    /* ---------------------------------------------------------------------- */
    /*                              internal helpers                          */
    /* ---------------------------------------------------------------------- */

    /// @notice Lower the external Euler LTV of a collateral in a target vault (prank its governor admin).
    function _lowerEulerExternalLTV(address targetVault, address collateral, uint16 borrowLTV, uint16 liqLTV) internal {
        vm.prank(IEVault(targetVault).governorAdmin());
        IEVault(targetVault).setLTV(collateral, borrowLTV, liqLTV, 0);
    }

    /// @notice Externally liquidate a CV on Euler as a funded third party (uses the EXTERNAL Euler EVC).
    function _eulerExternalLiquidate(CVHandles memory h, address liq, uint256 repayAssets) internal {
        // fund the liquidator with collateral so its post-liquidation account stays healthy
        _dealEToken(h.collateralAsset, h.collateralUnderlying, liq, 100 ether);
        address eulerEVC = IEVault(h.targetVault).EVC();
        vm.startPrank(liq);
        IEVC(eulerEVC).enableCollateral(liq, h.collateralAsset);
        IEVC(eulerEVC).enableController(liq, h.targetVault);
        IEVault(h.targetVault).liquidate(h.cv, h.collateralAsset, repayAssets, 0);
        vm.stopPrank();
    }

    /// @notice Mint eToken shares to `to` by wrapping underlying (bounded by the eToken supply cap).
    function _dealEToken(address eToken, address underlying, address to, uint256 maxUnderlying) internal {
        uint256 headroom = _supplyCapHeadroom(eToken);
        uint256 amt = maxUnderlying < headroom ? maxUnderlying : headroom * 8 / 10;
        deal(underlying, to, amt);
        vm.startPrank(to);
        IERC20(underlying).approve(eToken, type(uint256).max);
        IEVault(eToken).deposit(amt, to);
        vm.stopPrank();
    }

    /// @notice Supply fresh credit into an Euler intermediate vault as a CLP, so a new CV has credit to
    ///         reserve regardless of live utilization. Bounded by the IV's supply-cap headroom.
    function _topUpEulerIV(address iv, address eToken, address underlying) internal {
        uint256 headroom = _supplyCapHeadroom(iv); // in IV-asset (eToken) units
        if (headroom == 0) return; // at cap; rely on whatever cash already exists
        uint256 targetShares = headroom * 8 / 10; // leave a buffer under the cap

        address clp = makeAddr("twyneCLP");
        // eTokens appreciate vs underlying, so 1 underlying -> <=1 share; wrap a little extra to be safe.
        uint256 underlyingToWrap = targetShares * 12 / 10;
        deal(underlying, clp, underlyingToWrap);
        vm.startPrank(clp);
        IERC20(underlying).approve(eToken, type(uint256).max);
        uint256 shares = IEVault(eToken).deposit(underlyingToWrap, clp);
        uint256 toDeposit = shares < targetShares ? shares : targetShares;
        IERC20(eToken).approve(iv, type(uint256).max);
        IEVault(iv).deposit(toDeposit, clp);
        vm.stopPrank();
    }

    /// @notice Remaining supply-cap headroom of an EVault in asset units (type(uint256).max if uncapped,
    ///         but clamped to a sane fixed amount so callers never wrap an absurd amount of underlying).
    function _supplyCapHeadroom(address vault) internal view returns (uint256) {
        (uint16 supplyCapRaw,) = IEVault(vault).caps();
        if (supplyCapRaw == 0) return 50 ether; // uncapped -> use a fixed modest top-up
        // EVK AmountCap: amount = 10**(raw & 63) * (raw >> 6) / 100
        uint256 cap = (10 ** (supplyCapRaw & 63)) * (supplyCapRaw >> 6) / 100;
        uint256 used = IEVault(vault).totalAssets();
        return cap > used ? cap - used : 0;
    }

    /// @notice Supply fresh credit into an Aave intermediate vault as a CLP (bounded by supply-cap headroom).
    function _topUpAaveIV(address iv, address wrapper, address underlying) internal {
        uint256 headroom = _supplyCapHeadroom(iv);
        if (headroom == 0) return;
        uint256 targetShares = headroom * 8 / 10;

        address clp = makeAddr("twyneAaveCLP");
        uint256 underlyingToWrap = targetShares * 12 / 10;
        deal(underlying, clp, underlyingToWrap);
        vm.startPrank(clp);
        IERC20(underlying).approve(wrapper, type(uint256).max);
        uint256 shares = IEVault(wrapper).deposit(underlyingToWrap, clp); // ERC4626 wrapper deposit
        uint256 toDeposit = shares < targetShares ? shares : targetShares;
        IERC20(wrapper).approve(iv, type(uint256).max);
        IEVault(iv).deposit(toDeposit, clp);
        vm.stopPrank();
    }

    /// @notice Override an Aave Chainlink price feed (vm.etch a MockAaveFeed) and drop the price to
    ///         `pct`% of its live value. Returns the feed address and the original answer for restore.
    function _dropAaveFeed(address underlying, uint256 pct) internal returns (address feed, uint256 origAnswer) {
        address oracle = IAaveAddressProvider(IAaveV3Pool(AAVE_POOL).ADDRESSES_PROVIDER()).getPriceOracle();
        feed = IAaveOracle(oracle).getSourceOfAsset(underlying);
        origAnswer = uint256(IAggregator(feed).latestAnswer());
        require(origAnswer > 0, "could not read live aave feed");
        vm.etch(feed, address(new MockAaveFeed()).code);
        MockAaveFeed(feed).setPrice(origAnswer * pct / 100);
    }

    /// @notice Restore a previously-etched Aave feed to a given answer.
    function _setAaveFeed(address feed, uint256 answer) internal {
        MockAaveFeed(feed).setPrice(answer);
    }

    /// @notice Repay a CV's entire remaining Aave debt as a funded third party (Aave allows repay onBehalfOf).
    /// @dev Aave forbids type(uint).max repay on behalf of another account, so pass an explicit amount that
    ///      exceeds the debt (Aave caps it to the outstanding debt, clearing it fully).
    function _aaveRepayDebt(CVHandles memory h) internal {
        address repayer = makeAddr("twyneAaveRepayer");
        uint256 amount = IAaveCV(h.cv).maxRepay() + 1 ether;
        deal(h.targetAsset, repayer, amount);
        vm.startPrank(repayer);
        IERC20(h.targetAsset).approve(AAVE_POOL, type(uint256).max);
        IAaveV3Pool(AAVE_POOL).repay(h.targetAsset, amount, 2, h.cv);
        vm.stopPrank();
    }

    /// @notice Externally liquidate an Aave CV as a funded third party (repays debt, receives collateral).
    function _aaveExternalLiquidate(CVHandles memory h, address liq, uint256 debtToCover) internal {
        deal(h.targetAsset, liq, 100 ether); // WETH to repay; Aave caps actual repay to the position's debt
        vm.startPrank(liq);
        IERC20(h.targetAsset).approve(AAVE_POOL, type(uint256).max);
        IAaveV3Pool(AAVE_POOL).liquidationCall(h.collateralUnderlying, h.targetAsset, h.cv, debtToCover, false);
        vm.stopPrank();
    }
}
