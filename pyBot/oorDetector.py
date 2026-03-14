import time

from logger import setup_logger


class oorDetector:
    def __init__(self, upperPrice, lowerPrice, thresholdScore, k):
        self.upperPrice = upperPrice
        self.lowerPrice = lowerPrice
        self.thresholdScore = thresholdScore
        # スコア減衰係数:k
        self.k = k
        # 積算スコア値の初期化
        self.accumScore = 0
        # インスタンス生成時の時刻を初期時間として記録
        self.lastTime = time.time()
        # レンジアウトしたときのフラグ
        self.isOutOfRange = False
        # logを残す
        self.log = setup_logger("OorDetector.log")
        self.log.info("set OutOfRangeDetector \n")
        self.log.info(f"Pa:{self.lowerPrice}, Pb:{self.upperPrice} \n")

    def getDeltaT(self):
        currentTime = time.time()
        # 現在時刻から最終実行時刻を引いた秒数をDeltaTとする
        DeltaT = currentTime - self.lastTime
        # 最終実行時刻を現在時刻で上書き(次のループで用いる)
        self.lastTime = currentTime
        # 戻り値
        return DeltaT

    def ifRangedOutUpper(self, currentPrice, deltaT):
        if currentPrice > self.upperPrice:
            self.log.info("current price went out of upper range")
            self.isOutOfRange = True
            # レンジ端からの乖離値の２乗を時刻で積分した値をスコアにする
            self.accumScore += (
                (currentPrice - self.upperPrice) / self.upperPrice
            ) ** 2 * deltaT

    def ifRangedOutLower(self, currentPrice, deltaT):
        if currentPrice < self.lowerPrice:
            self.log.info("current price went out of lower range")
            self.isOutOfRange = True
            self.accumScore += (
                (self.lowerPrice - currentPrice) / self.lowerPrice
            ) ** 2 * deltaT

    def modiScoreForNextStep(self):
        if not self.isOutOfRange:
            # レンジ内に戻っている場合には古いスコアは次のステップでkを乗じて減衰させておく
            self.accumScore = self.k * self.accumScore
        self.isOutOfRange = False

    # Detector起動、リポジションが必要な場合にはTrueを返す
    def runDetector(self, currentPrice):
        ret = False
        deltaT = self.getDeltaT()
        self.ifRangedOutUpper(currentPrice=currentPrice, deltaT=deltaT)
        self.ifRangedOutLower(currentPrice=currentPrice, deltaT=deltaT)
        if self.accumScore > self.thresholdScore:
            self.log.info("Required Rebalancing for Position in Liquidity Pool \n")
            ret = True
            self.accumScore = 0
        self.modiScoreForNextStep()  # しきい値チェックが終了したら次のループ用にスコアを更新
        return ret
