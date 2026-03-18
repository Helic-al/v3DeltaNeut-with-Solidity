import csv
import datetime
import os
import time

from dotenv import load_dotenv
from mainbot import SafeRealBot
from web3 import Web3

load_dotenv("./.env")
# --- Uniswapコントラクト設定 (Arbitrum) ---
# 1. Pool Contract (価格取得用)
POOL_ADDRESS = "0xC6962004f452bE9203591991D15f6b388e09E8D0"  # ETH/USDC 0.05%
POOL_ABI = '[{"inputs":[],"name":"slot0","outputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"observationIndex","type":"uint16"},{"internalType":"uint16","name":"observationCardinality","type":"uint16"},{"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"},{"internalType":"uint8","name":"feeProtocol","type":"uint8"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"}]'
ARB_POOL_ADDRESS = Web3.to_checksum_address(
    "0xcda53b1f66614552f834ceef361a8d12a0b8dad8"
)

RPC_URL = os.environ.get("INFURA_RPC_URL")


class transPrice(SafeRealBot):
    def getDexPrice(self):
        # プールコントラクト（例: WETH/USDC 0.05%プール）のインスタンスを作成
        self.pool_contract = self.w3.eth.contract(address=POOL_ADDRESS, abi=POOL_ABI)

        # slot0を呼び出して現在の状態を取得（ガス代無料）
        slot0 = self.pool_contract.functions.slot0().call()
        sqrtPriceX96 = slot0[0]

        # sqrtPriceX96 から実際の価格への変換公式
        # (sqrtPriceX96 / 2^96)^2 * (10^token0_decimals / 10^token1_decimals)
        # ※WETH(18桁) と USDC(6桁) の場合
        price = (sqrtPriceX96 / (2**96)) ** 2
        adjusted_price = price * (
            10**18 / 10**6
        )  # トークンの順番によって適宜反転させます

        print(f"現在のDEX Mid価格: {adjusted_price}")
        return adjusted_price


if __name__ == "__main__":
    tp = transPrice()

    tp.w3 = Web3(Web3.HTTPProvider(RPC_URL))

    arb = transPrice()

    arb.w3 = Web3(Web3.HTTPProvider(RPC_URL))
    arb.pool_contract = arb.w3.eth.contract(address=ARB_POOL_ADDRESS, abi=POOL_ABI)

    while True:
        now = datetime.datetime.now()
        dp = tp.getDexPrice()
        cp = tp.get_cex_price()

        arbDP = arb.getDexPrice()
        arbcp = arb.get_cex_price(coin="ARB")

        newRow = {
            "timestamp": now,
            "ETH_DEX": dp,
            "ETH_CEX": cp,
            "ARB_DEX": arbDP,
            "ARB_CEX": arbcp,
        }

        fieldNames = ["timestamp", "ETH_DEX", "ETH_CEX", "ARB_DEX", "ARB_CEX"]

        with open("ethAndArb.csv", mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldNames)

            if f.tell() == 0:
                writer.writeheader()

            writer.writerow(newRow)

        time.sleep(10)
