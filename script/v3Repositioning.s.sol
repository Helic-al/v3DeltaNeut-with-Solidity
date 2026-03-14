// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;
// 


// インターフェースの準備
INonfungiblePositionManager nfpm = INonfungiblePositionManager(0xC36442b4a4522E871399CD717aBDD847Ab11FE88);
ISwapRouter router = ISwapRouter(0xE592427A0AEce92De3Edee1F18E0157C05861564);


contract Reposition is Script {
    // ---------------------------------------------------
    // ① Withdraw (流動性の引き出しと回収)
    // ---------------------------------------------------
    // 1. まず流動性を減らす (decreaseLiquidity)
    nfpm.decreaseLiquidity(INonfungiblePositionManager.DecreaseLiquidityParams({
        tokenId: myTokenId, // 現在持っているV3ポジションのNFT ID
        liquidity: liquidityToRemove,
        amount0Min: 0,
        amount1Min: 0,
        deadline: block.timestamp
    }));

    // 2. 減らしたトークン（と未収穫の手数料）をウォレットに回収する (collect)
    nfpm.collect(INonfungiblePositionManager.CollectParams({
        tokenId: myTokenId,
        recipient: address(this), // 一旦このコントラクトで受け取る
        amount0Max: type(uint128).max,
        amount1Max: type(uint128).max
    }));

    // ---------------------------------------------------
    // ② Swap (比率の調整)
    // ---------------------------------------------------
    // ※Pythonから渡された「どちらのトークンをどれだけSwapするか」の計算結果を使う
    // スワップルーターにトークンの使用許可(Approve)を出す
    IERC20(tokenIn).approve(address(router), amountToSwap);

    router.exactInputSingle(ISwapRouter.ExactInputSingleParams({
        tokenIn: tokenIn,
        tokenOut: tokenOut,
        fee: 500, // 0.05%プールの場合
        recipient: address(this),
        deadline: block.timestamp,
        amountIn: amountToSwap,
        amountOutMinimum: 0,
        sqrtPriceLimitX96: 0
    }));

    // ---------------------------------------------------
    // ③ Add Liquidity (新しいレンジへの流動性追加)
    // ---------------------------------------------------
    // NFPMに両方のトークンの使用許可(Approve)を出す
    IERC20(token0).approve(address(nfpm), amount0ToAdd);
    IERC20(token1).approve(address(nfpm), amount1ToAdd);

    // 新しいTickでポジションを作成 (mint)
    (uint256 newTokenId, , , ) = nfpm.mint(INonfungiblePositionManager.MintParams({
        token0: token0,
        token1: token1,
        fee: 500,
        tickLower: newTickLower,
        tickUpper: newTickUpper,
        amount0Desired: amount0ToAdd,
        amount1Desired: amount1ToAdd,
        amount0Min: 0,
        amount1Min: 0,
        recipient: address(this), // 新しいNFTの所有者
        deadline: block.timestamp
    }));
}       