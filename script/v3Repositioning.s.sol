// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";
import {console} from "forge-std/console.sol";

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function allowance(address owner, address spender) external view returns (uint256);
}

interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn; address tokenOut; uint24 fee; address recipient; uint256 deadline;
        uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

interface INonfungiblePositionManager {
    function ownerOf(uint256 tokenId) external view returns (address);

    function positions(uint256 tokenId) external view returns (
        uint96 nonce, address operator, address token0, address token1, uint24 fee, int24 tickLower, int24 tickUpper,
        uint128 liquidity, uint256 feeGrowthInside0LastX128, uint256 feeGrowthInside1LastX128, uint128 tokensOwed0, uint128 tokensOwed1
    );
    struct DecreaseLiquidityParams { uint256 tokenId; uint128 liquidity; uint256 amount0Min; uint256 amount1Min; uint256 deadline; }
    function decreaseLiquidity(DecreaseLiquidityParams calldata params) external payable returns (uint256 amount0, uint256 amount1);
    struct CollectParams { uint256 tokenId; address recipient; uint128 amount0Max; uint128 amount1Max; }
    function collect(CollectParams calldata params) external payable returns (uint256 amount0, uint256 amount1);
    struct MintParams {
        address token0; address token1; uint24 fee; int24 tickLower; int24 tickUpper;
        uint256 amount0Desired; uint256 amount1Desired; uint256 amount0Min; uint256 amount1Min;
        address recipient; uint256 deadline;
    }
    function mint(MintParams calldata params) external payable returns (uint256 tokenId, uint128 liquidity, uint256 amount0, uint256 amount1);
}

contract Reposition is Script {
    INonfungiblePositionManager constant NFPM = INonfungiblePositionManager(0xC36442b4a4522E871399CD717aBDD847Ab11FE88);
    ISwapRouter constant ROUTER = ISwapRouter(0xE592427A0AEce92De3Edee1F18E0157C05861564);
    address constant WETH = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1;
    address constant USDC = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831;
    uint24 constant POOL_FEE = 500;

    function run() external {
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        address deployerAddress = vm.addr(deployerPrivateKey);

        vm.startBroadcast(deployerPrivateKey);

        {
            if (!vm.envBool("SKIP_WITHDRAW")) {
                uint256 oldTokenId = vm.envUint("OLD_TOKEN_ID");
                if (oldTokenId != 0) _withdraw(oldTokenId, deployerAddress);
            }
        }

        {
            uint256 swapAmount = vm.envUint("SWAP_AMOUNT");
            if (swapAmount > 0) {
                _swap(swapAmount, vm.envBool("ZERO_FOR_ONE"), deployerAddress);
            }
        }

        {
            uint256 newTokenId = _addLiquidity(
                int24(vm.envInt("NEW_TICK_LOWER")),
                int24(vm.envInt("NEW_TICK_UPPER")),
                deployerAddress
            );
            console.log("=== REPOSITION_RESULT ===");
            console.log("NEW_TOKEN_ID:", newTokenId);
        }

        vm.stopBroadcast();
    }

    function _withdraw(uint256 oldTokenId, address recipient) internal {
        console.log("Starting Withdraw Phase for Token ID:", oldTokenId);
     
        address nftOwner = NFPM.ownerOf(oldTokenId);
        console.log("NFT Actual Owner:", nftOwner);
        console.log("Script Executing As:", recipient);
        
        require(nftOwner == recipient, "ERROR: You are not the owner of this NFT!");
        
        uint128 liq;
        {
            (,,,,,,, liq, , , , ) = NFPM.positions(oldTokenId);
        }

        if (liq > 0) {
            NFPM.decreaseLiquidity(INonfungiblePositionManager.DecreaseLiquidityParams({
                tokenId: oldTokenId, liquidity: liq, amount0Min: 0, amount1Min: 0, deadline: block.timestamp+300
            }));
            NFPM.collect(INonfungiblePositionManager.CollectParams({
                tokenId: oldTokenId, recipient: recipient, amount0Max: type(uint128).max, amount1Max: type(uint128).max
            }));
            console.log("Withdraw & Collect successful.");
        }
    }

    function _swap(uint256 swapAmount, bool zeroForOne, address recipient) internal {
        console.log("Starting Swap Phase. Amount:", swapAmount);
        address tokenIn = zeroForOne ? WETH : USDC;
        address tokenOut = zeroForOne ? USDC : WETH;

        IERC20(tokenIn).approve(address(ROUTER), swapAmount);

        ROUTER.exactInputSingle(ISwapRouter.ExactInputSingleParams({
            tokenIn: tokenIn, tokenOut: tokenOut, fee: POOL_FEE, recipient: recipient,
            deadline: block.timestamp+300, amountIn: swapAmount, amountOutMinimum: 0, sqrtPriceLimitX96: 0
        }));
        console.log("Swap successful.");
    }
    
    function _addLiquidity(int24 newTickLower, int24 newTickUpper, address recipient) internal returns (uint256) {
        console.log("Starting Add Liquidity...");
        uint256 bal0 = IERC20(WETH).balanceOf(recipient);
        uint256 bal1 = IERC20(USDC).balanceOf(recipient);

        // 💡 [修正ポイント] STF対策：シミュレーションとのズレを吸収するため、
        // 実際の残高の「99.5%」を投入希望額（Desired）として設定する
        uint256 desired0 = (bal0 * 995) / 1000;
        uint256 desired1 = (bal1 * 995) / 1000;

        // Approveは実際の残高の全額を通しておく
        IERC20(WETH).approve(address(NFPM), bal0);
        IERC20(USDC).approve(address(NFPM), bal1);

        // 💡 amountDesired に bal ではなく desired を渡すように変更
        (uint256 newTokenId, , , ) = NFPM.mint(INonfungiblePositionManager.MintParams({
            token0: WETH, token1: USDC, fee: POOL_FEE, tickLower: newTickLower, tickUpper: newTickUpper,
            amount0Desired: desired0, amount1Desired: desired1, amount0Min: 0, amount1Min: 0,
            recipient: recipient, deadline: block.timestamp + 300
        }));
        
        // ダストのApproveリセット処理
        if (IERC20(WETH).allowance(recipient, address(NFPM)) > 0) IERC20(WETH).approve(address(NFPM), 0);
        if (IERC20(USDC).allowance(recipient, address(NFPM)) > 0) IERC20(USDC).approve(address(NFPM), 0);

        return newTokenId;
    }
}
