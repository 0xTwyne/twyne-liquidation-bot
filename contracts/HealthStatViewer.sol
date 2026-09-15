// SPDX-License-Identifier: MIT

pragma solidity ^0.8.28;

interface IEVault {
    function accountLiquidity(address account, bool liquidation)
        external
        view
        returns (uint256 collateralValue, uint256 liabilityValue);
    function LTVLiquidation(address collateral) external view returns (uint16);
    function asset() external view returns (address);
    function unitOfAccount() external view returns (address);
    function debtOf(address account) external view returns (uint256);
    /// @dev Twyne 1.0.7: collateral vaults price collateral with the intermediate vault's router.
    function oracle() external view returns (address);
}

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
}

interface IAToken {
    function scaledBalanceOf(address account) external view returns (uint256);
}

interface IAaveV3Pool {
    function getUserAccountData(address user)
        external
        view
        returns (
            uint256 totalCollateralBase,
            uint256 totalDebtBase,
            uint256 availableBorrowsBase,
            uint256 currentLiquidationThreshold,
            uint256 ltv,
            uint256 healthFactor
        );
    function getReserveData(address asset) external view returns (ReserveData memory);
    function getEModeCategoryCollateralBitmap(uint8 id) external view returns (uint128);
    function getEModeCategoryCollateralConfig(uint8 id) external view returns (CollateralConfig memory);
    function ADDRESSES_PROVIDER() external view returns (IAaveV3AddressesProvider);
}

interface IAaveV3AddressesProvider {
    function getPoolDataProvider() external view returns (address);
}

/// @dev MUST mirror the live Aave V3 mainnet Pool's `getReserveData` return type
/// (Aave's `DataTypes.ReserveDataLegacy`). The fields and their exact order — including
/// `currentStableBorrowRate` and `stableDebtTokenAddress` — are load-bearing for ABI
/// decoding: a struct that omits or reorders them silently misaligns the decode (e.g. a
/// dirty `id` with high bits set), which trips Solidity 0.8 ABI validation and reverts
/// inside `_aaveExtLiqLTV()` for every eMode Aave collateral vault. Note this legacy
/// layout has NO trailing `virtualUnderlyingBalance`; that field belongs to the newer
/// non-legacy struct and must not be appended, or the decode overruns the returned buffer.
struct ReserveData {
    //stores the reserve configuration
    uint256 configuration;
    //the liquidity index. Expressed in ray
    uint128 liquidityIndex;
    //the current supply rate. Expressed in ray
    uint128 currentLiquidityRate;
    //variable borrow index. Expressed in ray
    uint128 variableBorrowIndex;
    //the current variable borrow rate. Expressed in ray
    uint128 currentVariableBorrowRate;
    //the current stable borrow rate. Expressed in ray
    uint128 currentStableBorrowRate;
    uint40 lastUpdateTimestamp;
    //the id of the reserve
    uint16 id;
    //aToken address
    address aTokenAddress;
    //stableDebtToken address
    address stableDebtTokenAddress;
    //variableDebtToken address
    address variableDebtTokenAddress;
    //address of the interest rate strategy
    address interestRateStrategyAddress;
    //the current treasury balance, scaled
    uint128 accruedToTreasury;
    //the outstanding unbacked aTokens minted through the bridging feature
    uint128 unbacked;
    //the outstanding debt borrowed against this asset in isolation mode
    uint128 isolationModeTotalDebt;
}

/// @dev MUST mirror Aave V3's `DataTypes.CollateralConfig` (the return type of
/// `getEModeCategoryCollateralConfig`). Field order is load-bearing: the layout
/// `{ltv, liquidationThreshold, liquidationBonus}` is returned by the pool, so reading
/// `.liquidationThreshold` requires it to sit in word 1. The prior order
/// `{liquidationThreshold, liquidationBonus, eModeCategory}` did NOT revert (same word
/// count) but silently returned word 0 — Aave's `ltv` — as the liquidation threshold,
/// giving a wrong externalLiqLTV / inHF for every eMode Aave vault.
struct CollateralConfig {
    uint16 ltv;
    uint16 liquidationThreshold;
    uint16 liquidationBonus;
}

struct PositionStats {
    uint256 userCollateralNative; // C  in cv.asset() units
    uint256 userCollateralUsd; // C  1e18
    uint256 reservedCreditNative; // C_LP = maxRelease(), cv.asset() units
    uint256 reservedCreditUsd; // C_LP 1e18
    uint256 borrowNative; // B  target-asset decimals
    uint256 borrowUsd; // B  1e18
    uint16 twyneLiqLTV; // liqLTV_t bps (MAXFACTOR=1e4) — capped parameter, <= 1e4
    uint32 twyneLTV; // LTV_t = B/C bps — unbounded ratio (can exceed 1e4 underwater)
    uint16 externalLiqLTV; // liqLTV_e bps — capped parameter, <= 1e4
    uint32 externalLTV; // LTV_e = B/(C+C_LP) bps — unbounded ratio
    uint256 extHF; // 1e18
    uint256 inHF; // 1e18
    uint16 maxTwyneLTV; // ~LTV_t^max bps — the protocol cap liqParams(iv, targetAsset).maxTwyneLiqLTV; twyneLiqLTV = min(chosen, this)
}

interface IAaveV3DataProvider {
    function getReserveConfigurationData(address asset)
        external
        view
        returns (
            uint256 decimals,
            uint256 ltv,
            uint256 liquidationThreshold,
            uint256 liquidationBonus,
            uint256 reserveFactor,
            bool usageAsCollateralEnabled,
            bool borrowingEnabled,
            bool isActive,
            bool isFrozen,
            bool isPaused
        );
}

interface IEulerRouter {
    function getQuote(uint256 amount, address base, address quote) external view returns (uint256);
}

interface ICollateralVaultBase {
    function targetVault() external view returns (address);
    function asset() external view returns (address);
    function intermediateVault() external view returns (IEVault);
    function twyneVaultManager() external view returns (IVaultManager);
    function totalAssetsDepositedOrReserved() external view returns (uint256);
    function maxRelease() external view returns (uint256);
    function twyneLiqLTV() external view returns (uint256);
    /// @dev Borrowed asset. Euler vaults: immutable, equals IEVault(targetVault).asset().
    /// Aave vaults: the asset the debt is denominated in, distinct from underlyingAsset().
    /// Together with intermediateVault() it is the key of the VaultManager 1.0.7
    /// liquidation parameters (liqParams).
    function targetAsset() external view returns (address);
}

interface IAaveV3CollateralVault is ICollateralVaultBase {
    function categoryId() external view returns (uint8);
    function underlyingAsset() external view returns (address);
    function aToken() external view returns (address);
}

interface IAaveV3ATokenWrapper {
    function latestAnswer() external view returns (int256);
    function decimals() external view returns (uint8);
}

/// @dev Twyne VaultManager 1.0.7 (V5). Liquidation parameters are keyed by
/// (intermediateVault, targetAsset). The one-argument getters of V4 are gone.
interface IVaultManager {
    function liqParams(address intermediateVault, address targetAsset)
        external
        view
        returns (uint16 externalLiqBuffer, uint16 maxTwyneLiqLTV, uint16 borrowBuffer);
}

library EModeConfiguration {
    function isReserveEnabledOnBitmap(uint128 bitmap, uint256 reserveIndex) internal pure returns (bool) {
        return (bitmap >> reserveIndex) & 1 != 0;
    }
}

library Math {
    function max(uint256 a, uint256 b) internal pure returns (uint256) {
        return a >= b ? a : b;
    }

    function min(uint256 a, uint256 b) internal pure returns (uint256) {
        return a <= b ? a : b;
    }
}

/// @title HealthStatViewer
/// @notice To contact the team regarding security matters, visit https://twyne.xyz/security
/// @dev functions to view liquidation health related stats of collateral vaults. Useful for frontend.
/// Targets Twyne contracts 1.0.7 (VaultManager V5, CollateralVaultFactory V5, Euler CV V4, Aave CV V3):
/// liquidation parameters come from VaultManager.liqParams(intermediateVault, targetAsset), the price
/// router from the intermediate vault's oracle(). It does not work against the 1.0.6 contracts.
contract HealthStatViewer {
    uint256 internal constant MAXFACTOR = 1e4;
    address public immutable aavePool;

    constructor(address _aavePool) {
        aavePool = _aavePool;
    }

    /// @dev Read the (externalLiqBuffer, maxTwyneLiqLTV) pair of a collateral vault from
    /// VaultManager.liqParams(intermediateVault, targetAsset). Mirrors CollateralVaultBase._liqParams()
    /// outside of a batch (snapshot == 0).
    function _liqParams(ICollateralVaultBase cv) internal view returns (uint256 buffer, uint256 maxTwyneLiqLTV) {
        (uint16 _buffer, uint16 _maxTwyneLiqLTV,) =
            cv.twyneVaultManager().liqParams(address(cv.intermediateVault()), cv.targetAsset());
        return (uint256(_buffer), uint256(_maxTwyneLiqLTV));
    }

    /// @dev The EulerRouter a collateral vault prices its collateral with: the intermediate
    /// vault's oracle. Mirrors EulerCollateralVault 1.0.7 (`EulerRouter(intermediateVault.oracle())`).
    function _router(IEVault intermediateVault) internal view returns (IEulerRouter) {
        return IEulerRouter(intermediateVault.oracle());
    }

    /// @dev 10 ** decimals of the collateral wrapper (AaveV3ATokenWrapper). Mirrors the
    /// AaveV3CollateralVault immutable `tenPowAssetDecimals`, which has no getter in 1.0.7.
    function _tenPowAssetDecimals(ICollateralVaultBase cv) internal view returns (uint256) {
        return 10 ** uint256(IAaveV3ATokenWrapper(cv.asset()).decimals());
    }

    /// @notice Check health factor from from Intermediate vault's perspective.
    /// @dev The Aave version of 1 / LTV is used in this implementation.
    function internalHF(address collateralVault)
        external
        view
        returns (uint256 healthFactor, uint256 collateralValue, uint256 liabilityValue)
    {
        (collateralValue, liabilityValue) =
            ICollateralVaultBase(collateralVault).intermediateVault().accountLiquidity(collateralVault, true);

        if (liabilityValue == 0) {
            healthFactor = type(uint256).max;
        } else {
            healthFactor = collateralValue * 1e18 / liabilityValue;
        }
    }

    /// @notice Check health factor from external protocol's perspective (excluding internal borrow).
    /// @notice HealthFactor's interpretation depends on the underlying protocol.
    /// On Aave = 1 / LTV, On Compound = debt_value - (liq_cf * collateral_value).
    /// @dev The Aave version of 1 / LTV is used in this implementation.
    function externalHF(address collateralVault)
        external
        view
        returns (uint256 healthFactor, uint256 collateralValue, uint256 liabilityValue)
    {
        address targetVault = ICollateralVaultBase(collateralVault).targetVault();
        if (targetVault == aavePool) {
            // For Aave, fetch health factor directly from the pool
            (collateralValue, liabilityValue,,,, healthFactor) =
                IAaveV3Pool(aavePool).getUserAccountData(collateralVault);
            // Aave returns asset values in 1e8 precision, multiply by 1e10 to match Euler precision (1e18)
            return (healthFactor, collateralValue * 1e10, liabilityValue * 1e10);
        }
        // Euler
        (collateralValue, liabilityValue) = IEVault(targetVault).accountLiquidity(collateralVault, true);

        if (liabilityValue == 0) {
            healthFactor = type(uint256).max;
        } else {
            healthFactor = collateralValue * 1e18 / liabilityValue;
        }
    }

    /// @notice Return health factors and debt values.
    /// @param collateralVault collateral vault address.
    /// @return extHF health factor from external protocol's perspective (1 / LTV).
    /// @return inHF health factor of from Twyne's perspective (1 / LTV).
    /// @return externalBorrowDebtValue external borrow value in targetVault unitOfAccount (usually USD).
    /// @return internalBorrowDebtValue internal borrow value in targetVault unitOfAccount (usually USD).
    function health(address collateralVault)
        external
        view
        returns (uint256 extHF, uint256 inHF, uint256 externalBorrowDebtValue, uint256 internalBorrowDebtValue)
    {
        ICollateralVaultBase cv = ICollateralVaultBase(collateralVault);
        if (cv.targetVault() == aavePool) {
            (extHF, inHF, externalBorrowDebtValue) = _healthAave(collateralVault);
        } else {
            (extHF, inHF, externalBorrowDebtValue) = _healthEuler(collateralVault);
        }

        (, internalBorrowDebtValue) = cv.intermediateVault().accountLiquidity(collateralVault, true);
    }

    /// @notice Return the full position bundle (collateral, debt, LTVs, health factors).
    /// @dev Additive view. Mirrors the same on-chain math as health()/internalHF()/externalHF();
    /// no protocol math is re-implemented off this contract. Field order is load-bearing —
    /// the Python bot unpacks PositionStats by position.
    /// @param collateralVault collateral vault address.
    /// @return s populated PositionStats. Native fields are in cv.asset()/target-asset units;
    /// *Usd fields are 1e18; *LTV fields are basis points vs MAXFACTOR (1e4).
    function positionStats(address collateralVault) external view returns (PositionStats memory s) {
        ICollateralVaultBase cv = ICollateralVaultBase(collateralVault);

        // Source collateral from the REAL token balance, not the tracked
        // `totalAssetsDepositedOrReserved`. The two are equal in every
        // non-liquidated state (the external-liquidation detector is defined as
        // `totalAssets <= realBalance`, and deposit/withdraw/skim keep them in
        // lockstep), so this is a no-op for live positions. They diverge ONLY
        // after an external liquidation, where the real balance is the correct,
        // post-fallback collateral and the tracked value is stale.
        //   - Euler: IERC20(asset()).balanceOf  (mirrors CollateralVaultBase detector)
        //   - Aave:  IAToken(aToken).scaledBalanceOf  (mirrors AaveV3CollateralVault detector)
        uint256 realBalance = cv.targetVault() == aavePool
            ? IAToken(IAaveV3CollateralVault(collateralVault).aToken()).scaledBalanceOf(collateralVault)
            : IERC20(cv.asset()).balanceOf(collateralVault);

        // Clamp the Credit-LP claim to what actually remains. This is a no-op in
        // the live state (maxRelease() <= totalAssets == realBalance always) and,
        // in the post-fallback state, keeps `userCollateralNative + reservedCreditNative
        // == realBalance` so consumers can reconstruct the true remaining balance.
        // The clamp never changes a downstream post-fallback split: that split's
        // release term is bounded by `realBalance - userCollateral <= realBalance`,
        // so any maxRelease >= realBalance behaves identically to realBalance.
        uint256 maxRel = cv.maxRelease();
        s.reservedCreditNative = Math.min(maxRel, realBalance);
        s.userCollateralNative = realBalance - s.reservedCreditNative;

        (s.extHF, s.inHF, s.borrowUsd,) = this.health(collateralVault);

        (, uint256 maxTwyne) = _liqParams(cv);
        s.twyneLiqLTV = uint16(Math.min(cv.twyneLiqLTV(), maxTwyne));
        s.maxTwyneLTV = uint16(maxTwyne);

        if (cv.targetVault() == aavePool) {
            _positionStatsAave(cv, collateralVault, s);
        } else {
            _positionStatsEuler(cv, collateralVault, s);
        }

        uint256 twyneLTV = s.userCollateralUsd == 0 ? 0 : s.borrowUsd * MAXFACTOR / s.userCollateralUsd;
        s.twyneLTV = _clampUint32(twyneLTV);
        uint256 totalColl = s.userCollateralUsd + s.reservedCreditUsd;
        uint256 externalLTV = totalColl == 0 ? 0 : s.borrowUsd * MAXFACTOR / totalColl;
        s.externalLTV = _clampUint32(externalLTV);
    }

    function _positionStatsEuler(ICollateralVaultBase cv, address collateralVault, PositionStats memory s)
        internal
        view
    {
        IEVault targetVault = IEVault(cv.targetVault());
        IEVault intermediateVault = cv.intermediateVault();
        address asset = cv.asset();
        address uoa = intermediateVault.unitOfAccount();
        IEulerRouter router = _router(intermediateVault);

        s.userCollateralUsd = router.getQuote(s.userCollateralNative, asset, uoa);
        s.reservedCreditUsd = router.getQuote(s.reservedCreditNative, asset, uoa);
        s.borrowNative = targetVault.debtOf(collateralVault);
        s.externalLiqLTV = targetVault.LTVLiquidation(asset);
    }

    /// @dev Aave branch. Values native collateral via the aToken wrapper price feed, mirroring
    /// AaveV3CollateralVault accounting (n * latestAnswer() / 10**assetDecimals gives Aave 1e8
    /// base value; *1e10 lifts to the 1e18 scale used by borrowUsd). externalLiqLTV reuses
    /// _aaveExtLiqLTV(). Implemented but covered by a separate Aave fork test (next task).
    function _positionStatsAave(ICollateralVaultBase cv, address collateralVault, PositionStats memory s)
        internal
        view
    {
        uint256 price = uint256(IAaveV3ATokenWrapper(cv.asset()).latestAnswer());
        uint256 tenPow = _tenPowAssetDecimals(cv);

        // n * price / tenPow is Aave-base (1e8); multiply by 1e10 to reach the 1e18 USD scale.
        s.userCollateralUsd = s.userCollateralNative * price * 1e10 / tenPow;
        s.reservedCreditUsd = s.reservedCreditNative * price * 1e10 / tenPow;

        // Native debt is denominated in the BORROWED asset (targetAsset()), not the
        // collateral underlyingAsset(). Read the variable debt token of targetAsset() so
        // borrowNative mirrors the Euler branch's targetVault.debtOf(cv) semantics.
        // (Using underlyingAsset() here returns 0 whenever the borrowed asset differs from
        // the collateral, which is the normal Twyne Aave case.)
        address borrowedAsset = cv.targetAsset();
        ReserveData memory rd = IAaveV3Pool(aavePool).getReserveData(borrowedAsset);
        s.borrowNative = IERC20(rd.variableDebtTokenAddress).balanceOf(collateralVault);

        s.externalLiqLTV = uint16(_aaveExtLiqLTV(collateralVault));
    }

    function _healthAave(address collateralVault)
        internal
        view
        returns (uint256 extHF, uint256 inHF, uint256 externalBorrowDebtValue)
    {
        ICollateralVaultBase cv = ICollateralVaultBase(collateralVault);
        (uint256 buffer, uint256 maxTwyneLiqLTV) = _liqParams(cv);
        (, uint256 liabilityValue,,,, uint256 healthFactor) = IAaveV3Pool(aavePool).getUserAccountData(collateralVault);

        // extHF tracks liquidation condition 1, where the external protocol's liquidation limit is nearly hit
        if (liabilityValue == 0) return (type(uint256).max, type(uint256).max, 0);
        if (healthFactor > type(uint256).max / MAXFACTOR) extHF = type(uint256).max;
        else extHF = buffer * healthFactor / MAXFACTOR;
        externalBorrowDebtValue = liabilityValue * 1e10; // multiply by 1e10 to match Euler precision (1e18)

        // inHF tracks liquidation condition 2 using AaveV3CollateralVault._canLiquidate() math
        uint256 adjExtLiqLTV = buffer * _aaveExtLiqLTV(collateralVault);
        uint256 collateralValueScaledByLiqLTV = _collateralScaledByLiqLTV1e8(cv, adjExtLiqLTV, maxTwyneLiqLTV)
            * uint256(IAaveV3ATokenWrapper(cv.asset()).latestAnswer())
            / _tenPowAssetDecimals(cv);
        inHF = collateralValueScaledByLiqLTV * 1e18 / (liabilityValue * MAXFACTOR * MAXFACTOR);
    }

    function _healthEuler(address collateralVault)
        internal
        view
        returns (uint256 extHF, uint256 inHF, uint256 externalBorrowDebtValue)
    {
        ICollateralVaultBase cv = ICollateralVaultBase(collateralVault);
        IEVault targetVault = IEVault(cv.targetVault());
        IEVault intermediateVault = cv.intermediateVault();
        (uint256 buffer, uint256 maxTwyneLiqLTV) = _liqParams(cv);
        uint256 externalCollateralValueScaledByLiqLTV;
        (externalCollateralValueScaledByLiqLTV, externalBorrowDebtValue) =
            targetVault.accountLiquidity(collateralVault, true);

        // extHF tracks liquidation condition 1, where the external protocol's liquidation limit is nearly hit
        if (externalBorrowDebtValue == 0) return (type(uint256).max, type(uint256).max, 0);
        extHF = buffer * 1e18 * externalCollateralValueScaledByLiqLTV / (MAXFACTOR * externalBorrowDebtValue);

        // inHF tracks liquidation condition 2 using EulerCollateralVault._canLiquidate() math
        uint256 adjExtLiqLTV = buffer * uint256(targetVault.LTVLiquidation(cv.asset()));
        uint256 collateralValueScaledByLiqLTV = _router(intermediateVault).getQuote(
            _collateralScaledByLiqLTV1e8(cv, adjExtLiqLTV, maxTwyneLiqLTV), cv.asset(), intermediateVault.unitOfAccount()
        );
        inHF = collateralValueScaledByLiqLTV * 1e18 / (externalBorrowDebtValue * MAXFACTOR * MAXFACTOR);
    }

    /// @dev Mirrors CollateralVaultBase._collateralScaledByLiqLTV1e8(isRebalancing=false, adjExtLiqLTV,
    /// maxTwyneLiqLTV, maxRelease()) of Twyne 1.0.7. The caller passes the cap read from liqParams.
    function _collateralScaledByLiqLTV1e8(ICollateralVaultBase cv, uint256 adjExtLiqLTV, uint256 maxTwyneLiqLTV)
        internal
        view
        returns (uint256)
    {
        uint256 totalAssets = cv.totalAssetsDepositedOrReserved();
        uint256 userCollateral = totalAssets - cv.maxRelease();

        return Math.max(
            adjExtLiqLTV * userCollateral,
            Math.min(adjExtLiqLTV * totalAssets, userCollateral * MAXFACTOR * Math.min(cv.twyneLiqLTV(), maxTwyneLiqLTV))
        );
    }

    /// @dev Mirrors AaveV3CollateralVault._getExtLiqLTV()
    function _aaveExtLiqLTV(address collateralVault) internal view returns (uint256) {
        IAaveV3CollateralVault cv = IAaveV3CollateralVault(collateralVault);
        uint8 categoryId = cv.categoryId();
        address underlyingAsset = cv.underlyingAsset();

        if (categoryId != 0) {
            uint256 reserveId = IAaveV3Pool(cv.targetVault()).getReserveData(underlyingAsset).id;
            uint128 collateralBitmap = IAaveV3Pool(cv.targetVault()).getEModeCategoryCollateralBitmap(categoryId);

            if (EModeConfiguration.isReserveEnabledOnBitmap(collateralBitmap, reserveId)) {
                return IAaveV3Pool(cv.targetVault()).getEModeCategoryCollateralConfig(categoryId).liquidationThreshold;
            }
        }

        // AaveV3CollateralVault 1.0.7 keeps its data provider as an internal immutable (no
        // getter). Resolve it the same way the vault does at construction:
        // IPool.ADDRESSES_PROVIDER().getPoolDataProvider().
        IAaveV3DataProvider dataProvider =
            IAaveV3DataProvider(IAaveV3Pool(cv.targetVault()).ADDRESSES_PROVIDER().getPoolDataProvider());
        (,, uint256 currentLiquidationThreshold,,,,,,,) = dataProvider.getReserveConfigurationData(underlyingAsset);
        return currentLiquidationThreshold;
    }

    function _clampUint32(uint256 value) internal pure returns (uint32) {
        return value > type(uint32).max ? type(uint32).max : uint32(value);
    }
}
