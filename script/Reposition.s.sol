// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script} from "forge-std/Script.sol";
import {console} from "forge-std/console.sol";
import {console} from "forge-std/console.sol";
import {IPoolManager, ModifyLiquidityParams, SwapParams} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {CurrencyLibrary, Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LPFeeLibrary} from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import {LiquidityAmounts} from "@uniswap/v4-core/test/utils/LiquidityAmounts.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {PoolId, PoolIdLibrary} from "@uniswap/v4-core/src/types/PoolId.sol";

// V4公式の流動性追加用ルーター
import {PoolModifyLiquidityTest} from "@uniswap/v4-core/src/test/PoolModifyLiquidityTest.sol";

// swap用にV3公式ルーター
import "@uniswap/v3-periphery/contracts/interfaces/ISwapRouter.sol";

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

contract Reposition is Script {
    using CurrencyLibrary for Currency;
    using StateLibrary for IPoolManager;
    using PoolIdLibrary for PoolKey;

    address constant POOL_MANAGER = 0xe54aCE66bD482c5781c9F69f89273586975FFcAC;
    
    // ★ あなたのHookアドレス
    address constant HOOK_ADDRESS = 0x351d40e706339c7D7588B6F915d62D42510fC080; 
    
    // ★ 【超重要】前回のログに出力されたルーターアドレスをここに貼ってください
    address constant OLD_ROUTER = 0x264C16Cd53412181c83B518e72d01a57ebfcF2bD; 

    address constant WETH_ADDRESS = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1;
    address constant USDC_ADDRESS = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831;

    // Arbitrum公式の Uniswap V3 SwapRouter アドレス
    address constant V3_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    
    int24 constant TICK_SPACING = 60;
    uint24 constant LP_FEE = LPFeeLibrary.DYNAMIC_FEE_FLAG; // 固定で作成してしまった500を指定してプールを特定します

    function run() external {
        // pythonから環境変数を受け取る
        int24 oldTickLower = int24(vm.envInt("DYNAMIC_OLD_LOWER"));
        int24 oldTickUpper = int24(vm.envInt("DYNAMIC_OLD_UPPER"));
        int24 newTickLower = int24(vm.envInt("DYNAMIC_NEW_LOWER"));
        int24 newTickUpper= int24(vm.envInt("DYNAMIC_NEW_UPPER"));
        int256 exactLiquidity = vm.envInt("EXACT_LIQUIDITY");
        bool isLiquidityZero = vm.envUint("IS_LIQUIDITY_ZERO") == 1;

        // wallet情報
        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        address myWallet = vm.addr(privateKey);

        int256 swapAmount = vm.envInt("SWAP_AMOUNT");
        bool zeroForOne = vm.envUint("SWAP_ZERO_FOR_ONE") == 1;

        console.log("--- Reposition Parameters ---");
        console.log("Old Range:", oldTickLower);
        console.log("to", oldTickUpper);
        console.log("New Range:", newTickLower);
        console.log("to", newTickUpper);
        console.log("Liquidity to Withdraw:", exactLiquidity);

        Currency token0 = Currency.wrap(WETH_ADDRESS);
        Currency token1 = Currency.wrap(USDC_ADDRESS);

        PoolKey memory key = PoolKey({
            currency0: token0,
            currency1: token1,
            fee: LP_FEE,
            tickSpacing: TICK_SPACING,
            hooks: IHooks(HOOK_ADDRESS)
        });

        vm.startBroadcast(privateKey);
            
        console.log("Withdrawing liquidity...");

        if (isLiquidityZero) {
            console.log("Current Liquidity is Zero. Skipping withdrawal...");
        } else {
            PoolModifyLiquidityTest(OLD_ROUTER).modifyLiquidity(
            key,
            ModifyLiquidityParams({
                tickLower: oldTickLower,
                tickUpper: oldTickUpper,
                liquidityDelta: -int256(uint256(exactLiquidity)), // マイナス指定で引き出し
                salt: bytes32(0)
            }),
            ""
            );
            console.log("Withdrawal Successful!");
        }

        
        // swap処理
        if (swapAmount < 0) {
            console.log("Executing Adjust Swap on Uniswap V3 ...");

            uint256 expectedOut = vm.envUint("DYNAMIC_SWAP_MIN_OUT");

            uint256 absAmount = uint256(-swapAmount);

            // 承認
            if (zeroForOne) {
                IERC20(WETH_ADDRESS).approve(V3_ROUTER, absAmount);
            } else {
                IERC20(USDC_ADDRESS).approve(V3_ROUTER, absAmount);
            }

            ISwapRouter.ExactInputSingleParams memory params = ISwapRouter.ExactInputSingleParams({
                tokenIn: zeroForOne ? WETH_ADDRESS : USDC_ADDRESS,
                tokenOut: zeroForOne ? USDC_ADDRESS : WETH_ADDRESS,
                fee: 500, // 例: Arbitrumで流動性の厚い 0.05% プール
                recipient: myWallet,
                deadline: block.timestamp + 60,
                amountIn: absAmount,
                amountOutMinimum: expectedOut, // 👈 0は絶対ダメ！計算した最低受け取り額を入れる
                sqrtPriceLimitX96: 0
            });

            ISwapRouter(V3_ROUTER).exactInputSingle(params);
            console.log("swap successful!");
        }

            
        // ここから流動性投入処理を記述

        PoolModifyLiquidityTest lpRouter = PoolModifyLiquidityTest(OLD_ROUTER);

        // // 本番用
        uint256 amount0Desired = IERC20(WETH_ADDRESS).balanceOf(myWallet);
        uint256 amount1Desired = IERC20(USDC_ADDRESS).balanceOf(myWallet);

        // テスト用
        // uint256 amount0Desired = 0.001 ether;
        // uint256 amount1Desired = 3 * 10**6;

        PoolId poolId = key.toId();
        (uint160 actualSqrtPriceX96, , , ) = IPoolManager(POOL_MANAGER).getSlot0(poolId);

        // 新規プール与える流動性を計算
        uint128 newLiquidity = LiquidityAmounts.getLiquidityForAmounts(
            actualSqrtPriceX96,
            TickMath.getSqrtPriceAtTick(newTickLower),
            TickMath.getSqrtPriceAtTick(newTickUpper),
            amount0Desired,
            amount1Desired
        );

        IERC20(WETH_ADDRESS).approve(address(lpRouter), (amount0Desired*11)/10);
        IERC20(USDC_ADDRESS).approve(address(lpRouter), (amount1Desired*11)/10);

        lpRouter.modifyLiquidity(
            key,
            ModifyLiquidityParams({
                tickLower: newTickLower,
                tickUpper: newTickUpper,
                liquidityDelta: int256(uint256(newLiquidity)),
                salt: bytes32(0)
            }),
            ""
        );
        console.log("Reposition Complete!");
        vm.stopBroadcast();
    }
}