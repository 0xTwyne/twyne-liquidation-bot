// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.4;

interface IEulerCollateralVault {
    error AssetMismatch();
    error CallerNotOwnerOrCollateralVaultFactory();
    error CannotRebalance();
    error ControllerDisabled();
    error EVC_InvalidAddress();
    error E_EmptyError();
    error E_TransferFromFailed(bytes errorPermit2, bytes errorTransferFrom);
    error EnforcedPause();
    error ExternalPositionUnhealthy();
    error ExternallyLiquidated();
    error HealthyNotLiquidatable();
    error IncorrectIndex();
    error IntermediateVaultAlreadySet();
    error IntermediateVaultNotSet();
    error InvalidInitialization();
    error NoLiquidationForZeroReserve();
    error NotAuthorized();
    error NotCollateralVault();
    error NotExternallyLiquidated();
    error NotInitializing();
    error NotIntermediateVault();
    error ReceiverNotBorrower();
    error ReceiverNotCollateralVault();
    error Reentrancy();
    error ReentrancyGuardReentrantCall();
    error RepayingMoreThanMax();
    error SafeERC20FailedOperation(address token);
    error SelfLiquidation();
    error SnapshotNotTaken();
    error T_CV_OperationDisabled();
    error T_OperationDisabled();
    error ValueOutOfRange();
    error VaultStatusLiquidatable();
    error ViolatorNotCollateralVault();

    event Initialized(uint64 version);
    event T_AddAllowedTargetVault(address indexed intermediateVault, address indexed targetVault);
    event T_Borrow(uint256 targetAmount, address indexed receiver);
    event T_CollateralVaultCreated(address indexed vault);
    event T_CollateralVaultInitialized();
    event T_ControllerDisabled();
    event T_Deposit(uint256 amount);
    event T_DepositUnderlying(uint256 amount);
    event T_DoCall(address indexed to, uint256 value, bytes data);
    event T_FactoryPause(bool pause);
    event T_HandleExternalLiquidation();
    event T_Rebalance();
    event T_RedeemUnderlying(uint256 amount, address indexed receiver);
    event T_RemoveAllowedTargetVault(address indexed intermediateVault, address indexed targetVault, uint256 index);
    event T_Repay(uint256 repayAmount);
    event T_SetBeacon(address indexed targetVault, address indexed beacon);
    event T_SetCollateralVaultFactory(address indexed factory);
    event T_SetCollateralVaultLiquidated(address indexed collateralVault, address indexed liquidator);
    event T_SetExternalLiqBuffer(address indexed collateralAddress, uint16 liqBuffer);
    event T_SetIntermediateVault(address indexed intermediateVault);
    event T_SetLTV(
        address indexed intermediateVault,
        address indexed collateralVault,
        uint16 borrowLimit,
        uint16 liquidationLimit,
        uint32 rampDuration
    );
    event T_SetMaxLiqLTV(address indexed collateralAddress, uint16 ltv);
    event T_SetOracleResolvedVault(address indexed collateralAddress, bool allow);
    event T_SetOracleRouter(address indexed newOracleRouter);
    event T_SetTwyneLiqLTV(uint256 ltv);
    event T_SetVaultManager(address indexed vaultManager);
    event T_Teleport(uint256 toDeposit, uint256 toBorrow);
    event T_Withdraw(uint256 amount, address indexed receiver);

    fallback() external;

    function EVC() external view returns (address);
    function asset() external view returns (address);
    function balanceOf(address user) external view returns (uint256);
    function borrow(uint256 _targetAmount, address _receiver) external;
    function borrower() external view returns (address);
    function canLiquidate() external view returns (bool);
    function canRebalance() external view returns (uint256);
    function checkAccountStatus(address, address[] memory) external view returns (bytes4 magicValue);
    function checkVaultStatus() external returns (bytes4 magicValue);
    function collateralVaultFactory() external view returns (address);
    function convertToAssets(uint256 shares) external pure returns (uint256);
    function deposit(uint256 assets) external;
    function depositUnderlying(uint256 underlying) external;
    function disableController() external;
    function eulerEVC() external view returns (address);
    function handleExternalLiquidation() external;
    function initialize(address __asset, address __borrower, uint256 __liqLTV, address __vaultManager) external;
    function intermediateVault() external view returns (address);
    function isExternallyLiquidated() external view returns (bool);
    function liquidate() external;
    function maxRelease() external view returns (uint256);
    function maxRepay() external view returns (uint256);
    function name() external view returns (string memory);
    function rebalance() external;
    function redeemUnderlying(uint256 assets, address receiver) external returns (uint256 underlying);
    function repay(uint256 _amount) external;
    function setTwyneLiqLTV(uint256 _ltv) external;
    function symbol() external view returns (string memory);
    function targetAsset() external view returns (address);
    function targetVault() external view returns (address);
    function teleport(uint256 toDeposit, uint256 toBorrow) external;
    function totalAssetsDepositedOrReserved() external view returns (uint256);
    function twyneLiqLTV() external view returns (uint256);
    function twyneVaultManager() external view returns (address);
    function collateralForBorrower(uint256 B, uint256 C) external view returns (uint256);
    function version() external pure returns (uint256);
    function withdraw(uint256 assets, address receiver) external;
}
