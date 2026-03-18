import datetime
import math
import os
import re
import subprocess

from logger import setup_logger


class PoolRepositioner:
    def __init__(
        self,
        inTokenId,
        inPrivateKey,
    ):
        """
        inTokenId: プール作成時に発行されたNFTの
        inRangeWidth: レンジ幅、入力した割合が上下レンジに設定される
        """
        self.TokenID = inTokenId
        self.privateKey = inPrivateKey
        self.log = setup_logger("PoolReposition.log")

    def commandExecuter(self, inCommand, inEnv_vars):
        """
        inCommandを実行する関数
        return (boolean, cmdResult, newTokenId)
        """

        try:
            subprocess.run(["forge", "clean"], capture_output=True)

            result = subprocess.run(
                inCommand,
                cwd="..",
                capture_output=True,
                text=True,
                check=True,
                env=inEnv_vars,
            )

            newTokenId = None

            match = re.search(r"NEW_TOKEN_ID:\s*(\d+)", result.stdout)
            if match:
                newTokenId = int(match.group(1))

            with open("reposition_history.log", "a", encoding="utf-8") as f:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"=== Reposition Executed: {now} ===\n")
                f.write(result.stdout)
                f.write("\n\n")

            self.log.info("✅ Reposition Success! Log saved to reposition_history.log")
            return (True, result.stdout, newTokenId)

        except subprocess.CalledProcessError as e:
            with open("reposition_history.log", "a", encoding="utf-8") as f:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"=== ❌ ERROR: {now} ===\n")
                f.write("--- 📜 STDOUT (console.logなどの詳細トレース) ---\n")
                f.write(e.stdout if e.stdout else "None")
                f.write(e.stderr)
                f.write("\n\n")

            self.log.error(f"❌ Reposition Failed:\n{e.stderr}")
            return (False, e.stderr, None)

    def calcNewTick(self, currentPrice):
        """_summary_
            currentPrice に基づいてレンジを計算

        Returns:
            return (newTickLower, newTickUpper)
        """
        tickSpacing = 10
        currentTick = int(math.log(currentPrice / 1e12, 1.0001))

        halfWidthTicks = 700  # 7.0%

        newTickLower = currentTick - halfWidthTicks
        newTickUpper = currentTick + halfWidthTicks

        newTickLower = (newTickLower // tickSpacing) * tickSpacing
        newTickUpper = (newTickUpper // tickSpacing) * tickSpacing

        return (currentTick, newTickLower, newTickUpper)

    def calc_approx_swap_amount(self, current_price, total_weth_amt, total_usdc_amt):
        """
        V3用シンプル版：手持ちの総資産（プール内+ウォレット）を50:50にするスワップ量を計算

        引数:
        - current_price: 現在のETH価格
        - total_weth_amt: プール内のWETH + ウォレットのWETH の合計枚数 (例: 1.5)
        - total_usdc_amt: プール内のUSDC + ウォレットのUSDC の合計枚数 (例: 3000.0)
        """

        # 1. 現在の資産のUSD価値を計算する
        weth_value_in_usd = total_weth_amt * current_price
        usdc_value_in_usd = total_usdc_amt

        total_value = weth_value_in_usd + usdc_value_in_usd
        target_value = total_value / 2.0  # 理想は半々(50:50)

        # デフォルトはスワップなし
        swap_zero_for_one = "true"
        swap_amount_wei = 0

        # 2. WETHが多すぎる場合 -> WETHを売る (WETH -> USDC)
        if weth_value_in_usd > target_value:
            excess_usd = weth_value_in_usd - target_value

            # 💡 差額が1ドル未満ならガス代の無駄なのでスワップしない
            if excess_usd > 1.0:
                weth_to_sell = excess_usd / current_price
                swap_zero_for_one = "true"
                # WETHは18桁なので 10**18 を掛ける
                swap_amount_wei = int(weth_to_sell * (10**18))

        # 3. USDCが多すぎる場合 -> USDCを売る (USDC -> WETH)
        elif usdc_value_in_usd > target_value:
            excess_usd = usdc_value_in_usd - target_value

            if excess_usd > 1.0:
                usdc_to_sell = excess_usd
                swap_zero_for_one = "false"
                # USDCは6桁なので 10**6 を掛ける
                swap_amount_wei = int(usdc_to_sell * (10**6))

        # Solidityに環境変数として渡しやすいように文字列で返す
        return swap_zero_for_one, str(swap_amount_wei)

    def executeReposition(
        self,
        rpcURL,
        inCurrentPrice,
        inTotalWETHamount,
        inTotalUSDCamount,
        inSkipWithdraw,
    ):
        """_summary_

        Args:
            rpcURL
            inCurrentPrice(usdc/eth)
        Returns:
            _type_: _description_
        """
        # 新規レンジを計算
        ticks = self.calcNewTick(currentPrice=inCurrentPrice)
        TickLower = ticks[1]
        TickUpper = ticks[2]

        env_vars = os.environ.copy()

        # 概算スワップ料を計算
        swap_zero_for_one, swap_amount = self.calc_approx_swap_amount(
            inCurrentPrice, inTotalWETHamount, inTotalUSDCamount
        )

        self.log.info(
            f"Swap Required: zeroForOne={swap_zero_for_one}, amount={swap_amount}"
        )

        # 環境変数の設定

        # TODO: v3プールリポジション時にコントラクトに渡す環境変数は以下
        # PRIVATE_KEY, SKIP_WITHDRAW, OLD_TOKEN_ID,
        # SWAP_AMOUNT, ZERO_FOR_ONE, NEW_TICK_LOWER, NEW_TICK_UPPER

        env_vars["PRIVATE_KEY"] = "0x" + str(self.privateKey)
        env_vars["NEW_TICK_LOWER"] = str(TickLower)
        env_vars["NEW_TICK_UPPER"] = str(TickUpper)
        env_vars["OLD_TOKEN_ID"] = str(self.TokenID)
        env_vars["ZERO_FOR_ONE"] = str(swap_zero_for_one)
        env_vars["SWAP_AMOUNT"] = str(swap_amount)
        env_vars["SKIP_WITHDRAW"] = str(inSkipWithdraw)

        command = [
            "forge",
            "script",
            "script/v3Repositioning.s.sol:Reposition",
            "--rpc-url",
            rpcURL,
            "--broadcast",
            "--private-key",
            self.privateKey,
            "-vvvv",
        ]

        response = self.commandExecuter(command, env_vars)

        if response[0]:
            self.log.info(f"successffully positioned new Pool \n {response[1]}")

            # TokenIDを更新
            self.TokenID = response[2]
            if self.TokenID is not None:
                self.log.info(f"TOKEN_ID :{self.TokenID}")
            else:
                self.log.warning(
                    "Reposition succeeded, but newTokenId was Not found in output."
                )

            return True

        else:
            self.log.error(f"Pool Repositioning FAILED \n {response[1]}")
            return False
