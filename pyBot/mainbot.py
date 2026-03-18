import datetime
import math
import os
import time
from decimal import Decimal

import boto3
import eth_account
import requests
from dotenv import load_dotenv
from getSecret import get_secret_key

# from hlOrder import HyperliquidOrderManager
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from logger import setup_logger
from lowPassFilter import LowPassFilter
from oorDetector import oorDetector
from v3Repositioner import PoolRepositioner
from web3 import Web3

load_dotenv("./.env")

# ログを残す
log = setup_logger("DeltaNeut.log")
orderLog = setup_logger(name="OrderLog", log_file="orderlog.log")

# --- ユーザー設定 ---
HL_PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY")
MAIN_ACCOUNT_ADDRESS = os.environ.get("ARB_WALLET_ADDRESS")
ARB_SECRET = get_secret_key()

alchemyKey = os.environ.get("ALCHEMY_KEY")  # 秘密鍵
TARGET_TOKEN_ID = int(
    os.environ.get("NFT_TOKEN", 0)
)  # ★ここにUniswapのToken IDを入れる
THRESHOLD = 0.13  # 初期リバランス閾値、dynamoDBの初回記録まではこの値を用いる
ALLOWABLE_RISK_PCT = 0.050  # 運用資金から許容するズレ(デルタETH)の割合
TARGET_RATIO = 0.5  # しきい値の何割までデルタを打ち消すか
MAX_RETRY = 3  # 指値注文のリトライ回数
RECORD_TIME = 300  # dynamoDBへの記録間隔(秒)

# aws設定
AWS_ACCESS_KEY = os.environ.get("AWS_KEY")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET")
REGION_NAME = "ap-northeast-1"
# --- インフラ設定 ---
RPC_URL = os.environ.get("ALCHEMY_RPC_URL")
HL_BASE_URL = constants.MAINNET_API_URL

WETH_ADDRESS = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC_ADDRESS = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"

# --- Uniswapコントラクト設定 (Arbitrum) ---
# 1. Pool Contract (価格取得用)
POOL_ADDRESS = "0xC6962004f452bE9203591991D15f6b388e09E8D0"
POOL_ABI = '[{"inputs":[],"name":"slot0","outputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"observationIndex","type":"uint16"},{"internalType":"uint16","name":"observationCardinality","type":"uint16"},{"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"},{"internalType":"uint8","name":"feeProtocol","type":"uint8"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"}]'

# 2. Position Manager (流動性Lとレンジ取得用)
NFPM_ADDRESS = "0xC36442b4a4522E871399CD717aBDD847Ab11FE88"
# mainv1.py の該当箇所を書き換え
# collect関数を含んだ完全なABI
NFPM_ABI = '[{"inputs":[{"internalType":"uint256","name":"tokenId","type":"uint256"}],"name":"positions","outputs":[{"internalType":"uint96","name":"nonce","type":"uint96"},{"internalType":"address","name":"operator","type":"address"},{"internalType":"address","name":"token0","type":"address"},{"internalType":"address","name":"token1","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"feeGrowthInside0LastX128","type":"uint256"},{"internalType":"uint256","name":"feeGrowthInside1LastX128","type":"uint256"},{"internalType":"uint128","name":"tokensOwed0","type":"uint128"},{"internalType":"uint128","name":"tokensOwed1","type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[{"components":[{"internalType":"uint256","name":"tokenId","type":"uint256"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint128","name":"amount0Max","type":"uint128"},{"internalType":"uint128","name":"amount1Max","type":"uint128"}],"internalType":"struct INonfungiblePositionManager.CollectParams","name":"params","type":"tuple"}],"name":"collect","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"payable","type":"function"}]'

# ERC20の残高を取得するための最小限のABI
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]

DISCORD_URL = os.environ.get("DISCORD_URL")


# --- Discord Embed用のカラーマップ ---
DISCORD_COLORS = {
    "success": 0x2ECC71,  # 緑
    "error": 0xE74C3C,  # 赤
    "warning": 0xF39C12,  # 黄
    "info": 0x3498DB,  # 青
    "default": 0x95A5A6,  # グレー
}


def _detect_color(message):
    """メッセージ内容から自動で色を判別"""
    if any(k in message for k in ("❌", "🛑", "FAILED", "Error")):
        return DISCORD_COLORS["error"]
    if any(k in message for k in ("🚨", "BAILOUT", "⚠")):
        return DISCORD_COLORS["warning"]
    if any(k in message for k in ("✅", "☁️", "🚀")):
        return DISCORD_COLORS["success"]
    return DISCORD_COLORS["info"]


def sendDiscord(message):
    """シンプルなEmbed形式でDiscordに通知"""
    try:
        embed = {
            "description": message,
            "color": _detect_color(message),
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }
        payload = {"embeds": [embed]}
        requests.post(DISCORD_URL, json=payload)
    except:
        pass


def sendDiscordReport(equity_data):
    """DynamoDB保存時にリッチEmbed形式で詳細レポートを送信"""
    try:
        embed = {
            "title": "📊 Delta Neutral Bot Report",
            "color": DISCORD_COLORS["success"],
            "fields": [
                {
                    "name": "💰 Total Equity",
                    "value": f"${equity_data['total_equity']:.2f}",
                    "inline": True,
                },
                {
                    "name": "📈 ETH Price",
                    "value": f"${equity_data['eth_price']:.2f}",
                    "inline": True,
                },
                {
                    "name": "🦄 Uniswap Value",
                    "value": f"${equity_data['uni_value']:.2f}",
                    "inline": True,
                },
                {
                    "name": "📊 HL Value",
                    "value": f"${equity_data['hl_value']:.2f}",
                    "inline": True,
                },
                {
                    "name": "💸 Funding Fees",
                    "value": f"${equity_data['funding_fees']:.4f}",
                    "inline": True,
                },
                {
                    "name": "📐 LP Delta",
                    "value": f"{equity_data.get('lp_delta', 0):.4f} ETH",
                    "inline": True,
                },
                {
                    "name": "🔄 Net Delta",
                    "value": f"{equity_data.get('net_delta', 0):.4f} ETH",
                    "inline": True,
                },
                {
                    "name": "📏 Raw Net Delta",
                    "value": f"{equity_data.get('raw_net_delta', 0):.4f} ETH",
                    "inline": True,
                },
                {
                    "name": "📊 Step PnL",
                    "value": f"${equity_data.get('step_pnl', 0):.4f}",
                    "inline": True,
                },
                {
                    "name": "📈 Cumulative PnL",
                    "value": f"${equity_data.get('cum_pnl', 0):.4f}",
                    "inline": True,
                },
            ],
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "footer": {"text": "Delta Neutral Bot V4"},
        }
        payload = {"embeds": [embed]}
        requests.post(DISCORD_URL, json=payload)
    except:
        pass


def get_price_from_sqrt(sqrt_pa):
    # 1. 2乗して「Raw Price (生の交換比率)」に戻す
    price_raw = sqrt_pa**2

    # 2. デシマル調整を行う
    # 計算式: price_raw * 10^(Token0_Decimals - Token1_Decimals)
    # WETH(18) - USDC(6) = 12桁の補正
    decimal_shift = 10 ** (18 - 6)  # つまり 1e12

    price_usd = price_raw * decimal_shift

    return price_usd


def format_decimal(val, precision=18):
    """
    floatをDynamoDB用のDecimalに安全に変換する。
    1. Noneチェック
    2. NaN/Infチェック
    3. 指数表記('1E-5')を防ぎ、固定小数点文字列にする
    4. DynamoDBの最小桁数未満のゴミ数値を0にする
    """
    if val is None:
        return None

    # float以外の型（numpy型など）が来たとき用にfloat化
    try:
        f_val = float(val)
    except Exception:
        return Decimal(0)

    # 無効値チェック
    if f_val != f_val:  # NaN check
        return None
    if f_val == float("inf") or f_val == float("-inf"):
        return None

    # 【重要】極小値（1e-30以下など）はDynamoDBでエラーになるため0にする
    # デルタニュートラル戦略なら、1e-15未満は実質誤差として0扱いで良いはず
    if abs(f_val) < 1e-15:
        return Decimal("0")

    # 【重要】str(val)ではなく、formatを使って指数表記「E」を回避する
    # {:.28f} などで十分な桁数を確保しつつ文字列化
    formatted_str = f"{f_val:.28f}"

    return Decimal(formatted_str)


def get_sqrt_from_price(price_usd):
    return math.sqrt(price_usd / (10**12))


# v6_3追加　pnlトラッカクラス
class DeltaPnLTracker:
    def __init__(self):
        self.cumulative_pnl = 0.0  # 累積損益 (ドル)
        self.last_price = None  # 前回の価格
        self.last_net_delta = 0.0  # 前回のデルタ (ETH枚数)

    def update(self, current_price, current_net_delta):
        """
        毎回のループで呼び出す
        Returns: (今回の変動損益, 累積損益)
        """
        # 初回起動時は計算できないのでスキップ
        if self.last_price is None:
            self.last_price = current_price
            self.last_net_delta = current_net_delta
            return 0.0, 0.0

        # 1. 価格変動幅を計算
        price_change = current_price - self.last_price

        # 2. 損益計算
        # 「その変動の間、前回のデルタを持っていた」と仮定して計算
        step_pnl = self.last_net_delta * price_change

        # 3. 累積に加算
        self.cumulative_pnl += step_pnl

        # 4. 次回用に値を更新
        self.last_price = current_price
        self.last_net_delta = current_net_delta

        return step_pnl, self.cumulative_pnl


class SafeRealBot:
    def __init__(self):
        # 1. 接続初期化
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.pool_contract = self.w3.eth.contract(address=POOL_ADDRESS, abi=POOL_ABI)
        self.nfpm_contract = self.w3.eth.contract(address=NFPM_ADDRESS, abi=NFPM_ABI)
        self.coin = "ETH"

        self.account = eth_account.Account.from_key(HL_PRIVATE_KEY)
        self.exchange = Exchange(
            self.account, HL_BASE_URL, account_address=MAIN_ACCOUNT_ADDRESS
        )
        self.info = Info(HL_BASE_URL, skip_ws=True)

        self.dynamodb = boto3.resource(
            "dynamodb",
            region_name=REGION_NAME,
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY,
        )
        self.table = self.dynamodb.Table("DeltaNeutralize")

        # nftIDは更新が入るので属性でも用意しておく
        self.currentTokenId = TARGET_TOKEN_ID
        # self.uniManager = UniswapManager(self.w3, ARB_SECRET)

        # 時間フィルタ
        self.firstBreachTime = None
        self.BailoutBreachTime = None

        # クールタイムを記録(bailout時には参照しない)
        self.cooltime = 0.0

        log.info(f"✅ Bot initialized for Address: {self.account.address}")
        log.info(f"🎯 Watching Uniswap Position Token ID: {TARGET_TOKEN_ID}")

        # CEX価格取得用ヘルパ関数

    def get_cex_price(self, coin="ETH"):
        # Hyperliquidから全銘柄の現在価格(Mark Price)を取得
        while True:
            mids = self.info.all_mids()
            if mids:
                cex_price = float(mids[coin])
                return cex_price
            else:
                print("Failed to get cexPrice, Trying again ...")
                time.sleep(10)

    def get_onchain_data(self):
        """価格、ユーザーの流動性(L)、ヘッジポジションを一括取得"""
        try:
            # --- A. Uniswap 現在価格 ---
            slot0 = self.pool_contract.functions.slot0().call()
            sqrtPriceX96 = slot0[0]

            # Q96 = 2^96
            Q96 = 2**96

            # 純粋なルート価格 (√Token1 / √Token0)
            sqrtP = sqrtPriceX96 / Q96

            # 表示用の価格 (USDCは6桁、ETHは18桁なので 10^12 で割る)
            # Price = (sqrtP^2) / (10^(18-6))
            human_price = (sqrtP**2) * 1e12

            # --- B. ユーザーのポジション情報 (NFPMから直接取得) ---
            # positions()の戻り値 (修正済みABIに対応):
            # 0:nonce, 1:operator, 2:token0, 3:token1, 4:fee,
            # 5:tickLower, 6:tickUpper, 7:liquidity ...
            pos_data = self.nfpm_contract.functions.positions(TARGET_TOKEN_ID).call()

            tick_lower = pos_data[5]  # 修正: ABI変更に伴いインデックス修正
            tick_upper = pos_data[6]  # 修正
            liquidity = pos_data[7]  # 修正: これが真の L (int)

            # Lをfloatに変換
            real_L = float(liquidity)

            # --- C. Hyperliquid 現在のヘッジ量 ---
            user_state = self.info.user_state(MAIN_ACCOUNT_ADDRESS)
            current_hedge = 0.0

            # AssetPositionsの中からETHを探す
            for pos in user_state["assetPositions"]:
                if pos["position"]["coin"] == "ETH":
                    current_hedge = float(pos["position"]["szi"])
                    break

            # log.info(
            #     {
            #         "sqrtP_raw": sqrtP,  # 計算用の純粋なルート価格
            #         "price": human_price,  # 表示用のドル価格
            #         "L": real_L,
            #         "tickLower": tick_lower,
            #         "tickUpper": tick_upper,
            #         "hedge_pos": current_hedge,
            #     }
            # )
            self.L = real_L
            self.tickLower = tick_lower
            self.tickUpper = tick_upper
            self.hedge_pos = current_hedge

            return {
                "sqrtP_raw": sqrtP,  # 計算用の純粋なルート価格
                "price": human_price,  # 表示用のドル価格
                "L": real_L,
                "tickLower": tick_lower,
                "tickUpper": tick_upper,
                "hedge_pos": current_hedge,
            }

        except Exception as e:
            # エラー時もプログラムを落とさず、Noneを返してリトライさせる
            log.info(f"Data Fetch Error: {e}")
            return None

    def get_token_amounts(self, liquidity, sqrtP, tick_lower, tick_upper):
        """流動性Lと価格から、現在のETHとUSDCの保有量を計算する"""
        # Q96 = 2**96

        sqrtPa = 1.0001 ** (tick_lower / 2)
        sqrtPb = 1.0001 ** (tick_upper / 2)

        amount0 = 0.0  # ETH
        amount1 = 0.0  # USDC

        # 1. 価格がレンジより下 (全額ETH)
        if sqrtP < sqrtPa:
            amount0 = liquidity * (1 / sqrtPa - 1 / sqrtPb)
            amount1 = 0.0
        # 2. 価格がレンジより上 (全額USDC)
        elif sqrtP >= sqrtPb:
            amount0 = 0.0
            amount1 = liquidity * (sqrtPb - sqrtPa)
        # 3. レンジ内 (混合)
        else:
            amount0 = liquidity * (1 / sqrtP - 1 / sqrtPb)
            amount1 = liquidity * (sqrtP - sqrtPa)

        return amount0 / 1e18, amount1 / 1e6

    def calcThreshold(self, total_equity, currentPrice):
        allowedRiskUSD = total_equity * ALLOWABLE_RISK_PCT

        if allowedRiskUSD < 15:
            allowedRiskUSD = 15

        thresholdETH = allowedRiskUSD / currentPrice

        return thresholdETH

    def get_total_equity(self):
        """UniswapとHyperliquidの合計資産価値(USD)を計算"""
        try:
            # --- 1. Uniswap側の資産 ---
            data = self.get_onchain_data()  # 既存の関数を活用
            if data is None or data["L"] == 0:
                return None

            # 現在のETH/USDC量
            eth_amount, usdc_amount = self.get_token_amounts(
                data["L"], data["sqrtP_raw"], data["tickLower"], data["tickUpper"]
            )

            MAX_UINT128 = 2**128 - 1
            collect_params = {
                "tokenId": TARGET_TOKEN_ID,
                "recipient": self.account.address,  # 誰宛でも良いが自分のアドレスにしておく
                "amount0Max": MAX_UINT128,  # 取れるだけ全部
                "amount1Max": MAX_UINT128,  # 取れるだけ全部
            }

            # .call() を使うことで、Txを投げずに戻り値(amount0, amount1)だけ取得
            # これが「流動性の中身以外」に溜まっている最新の手数料
            current_fees = self.nfpm_contract.functions.collect(collect_params).call()

            fees_eth = current_fees[0] / 1e18
            fees_usdc = current_fees[1] / 1e6

            # Uniswap合計価値 ($)
            # (ETH量 + 未回収ETH) * 価格 + (USDC量 + 未回収USDC)
            uni_value_usd = (eth_amount + fees_eth) * data["price"] + (
                usdc_amount + fees_usdc
            )

            # 現在の報酬手数料を独立して算出
            funding_fees = fees_eth * data["price"] + fees_usdc

            # --- 2. Hyperliquid側の資産 ---
            # main_address (資金が入っている口座) の情報を取得
            # user_state = self.info.user_state(MAIN_ACCOUNT_ADDRESS)

            # # marginSummary.accountValue が「証拠金 + 未実現PnL」の合計価値です
            # hl_value_usd = float(user_state["marginSummary"]["accountValue"])

            spot_state = self.info.spot_user_state(MAIN_ACCOUNT_ADDRESS)
            spot_usdc = 0.0
            for balance in spot_state.get("balances", []):
                if balance["coin"] == "USDC":
                    spot_usdc = float(balance["total"])
                    break

            # 2. 先物（Perp）APIから、現在のポジションの「含み損益（Unrealized PnL）」の合計を計算
            # user_state = self.info.user_state(MAIN_ACCOUNT_ADDRESS)
            # unrealized_pnl = 0.0
            # for position in user_state.get("assetPositions", []):
            #     pos_data = position.get("position", {})
            #     unrealized_pnl += float(pos_data.get("unrealizedPnl", 0.0))

            # 3. 現金残高に含み損益を足して、真の評価額とする
            hl_value_usd = spot_usdc

            # total_equity = uni_value_usd + hl_value_usd

            try:
                # ① 生のETH残高 (ガス代用など / 18 decimals)
                eth_wei = self.w3.eth.get_balance(MAIN_ACCOUNT_ADDRESS)
                eth_wallet = eth_wei / (10**18)

                # ② WETH残高 (18 decimals)
                weth_contract = self.w3.eth.contract(
                    address=WETH_ADDRESS, abi=ERC20_ABI
                )
                weth_wei = weth_contract.functions.balanceOf(
                    MAIN_ACCOUNT_ADDRESS
                ).call()
                weth_wallet = weth_wei / (10**18)

                # ③ USDC残高 (ArbitrumネイティブUSDCは 6 decimals)
                usdc_contract = self.w3.eth.contract(
                    address=USDC_ADDRESS, abi=ERC20_ABI
                )
                usdc_mwei = usdc_contract.functions.balanceOf(
                    MAIN_ACCOUNT_ADDRESS
                ).call()
                usdc_wallet = usdc_mwei / (10**6)

                # ウォレット内の総資産をUSD換算（ETHとWETHはCEX価格を掛ける）
                wallet_value_usd = (eth_wallet + weth_wallet) * data[
                    "price"
                ] + usdc_wallet

            except Exception as e:
                log.error(f"ウォレット残高の取得に失敗しました: {e}")
                wallet_value_usd = 0.0

            # ==========================================
            # 4. 最終的な総資産（Total Equity）の合算
            # ==========================================
            total_equity = uni_value_usd + hl_value_usd + wallet_value_usd

            # 総資産計算の際にショートのスレッショルドを再計算
            self.ETHthreshold = self.calcThreshold(
                total_equity=total_equity, currentPrice=data["price"]
            )

            return {
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "uni_value": uni_value_usd,
                "hl_value": hl_value_usd,
                "funding_fees": funding_fees,
                "total_equity": total_equity,
                "eth_price": data["price"],
            }

        except Exception as e:
            log.info(f"Equity Calc Error: {e}")
            return None

    def getWalletEth(self):
        try:
            # ① 生のETH残高 (ガス代用など / 18 decimals)
            eth_wei = self.w3.eth.get_balance(MAIN_ACCOUNT_ADDRESS)
            eth_wallet = eth_wei / (10**18)

            # ② WETH残高 (18 decimals)
            weth_contract = self.w3.eth.contract(address=WETH_ADDRESS, abi=ERC20_ABI)
            weth_wei = weth_contract.functions.balanceOf(MAIN_ACCOUNT_ADDRESS).call()
            weth_wallet = weth_wei / (10**18)

            return weth_wallet + eth_wallet

        except Exception as e:
            log.error(f"ウォレット残高の取得に失敗しました: {e}")
            return 0

    def getWalletWethAndUsdc(self):
        try:
            # WETHの枚数を取得
            # ② WETH残高 (18 decimals)
            weth_contract = self.w3.eth.contract(address=WETH_ADDRESS, abi=ERC20_ABI)
            weth_wei = weth_contract.functions.balanceOf(MAIN_ACCOUNT_ADDRESS).call()
            weth_wallet = weth_wei / (10**18)

            # ③ USDC残高 (ArbitrumネイティブUSDCは 6 decimals)
            usdc_contract = self.w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
            usdc_mwei = usdc_contract.functions.balanceOf(MAIN_ACCOUNT_ADDRESS).call()
            usdc_wallet = usdc_mwei / (10**6)

            return weth_wallet, usdc_wallet

        except Exception as e:
            log.error(f"ウォレット残高の取得に失敗しました: {e}")

    # mainbot.py または v3Repositioner.py に追加するイメージ

    def get_latest_token_id(self, owner_address=MAIN_ACCOUNT_ADDRESS):
        """
        ウォレットが所有している最新のUniswap V3 NFTのTokenIDを取得する
        """
        # NFPMのコントラクトインスタンスを作成（abiはERC721準拠のもの）
        nfpm_contract = self.w3.eth.contract(address=NFPM_ADDRESS, abi=NFPM_ABI)

        # 自分が持っているNFTの総数を取得
        balance = nfpm_contract.functions.balanceOf(owner_address).call()

        if balance == 0:
            return 0

        # インデックスは0から始まるため、(balance - 1) が最新のNFT
        latest_token_id = nfpm_contract.functions.tokenOfOwnerByIndex(
            owner_address, balance - 1
        ).call()

        return latest_token_id

    def calcRawDelta(self, currentPrice):
        # 定数定義
        DECIMALS_ETH = 1e18
        # Uniswap V3 公式式によるETH保有量計算 (Wei単位)
        raw_amount0_wei = 0.0

        sp = get_sqrt_from_price(currentPrice)
        sqrtPa = 1.0001 ** (self.tickLower / 2)
        sqrtPb = 1.0001 ** (self.tickUpper / 2)
        L = self.L
        hedge_pos = self.hedge_pos

        if sp < sqrtPa:
            # 現在価格 < レンジ (全額ETH)
            # amount0 = L * (1/√Pa - 1/√Pb)
            raw_amount0_wei = L * (1 / sqrtPa - 1 / sqrtPb)

        elif sp >= sqrtPb:
            # 現在価格 > レンジ (全額USDC, ETHは0)
            raw_amount0_wei = 0.0
        else:
            # レンジ内 (混合)
            # amount0 = L * (1/√P - 1/√Pb)
            raw_amount0_wei = L * (1 / sp - 1 / sqrtPb)

        raw_net_delta = raw_amount0_wei / DECIMALS_ETH + hedge_pos

        return raw_net_delta

    def execute_trade(self, amount_eth):
        """HLへ発注"""
        is_buy = amount_eth > 0
        sz = round(abs(amount_eth), 4)

        # --- 🚨 安全装置 (Failsafe) ---
        # 「最大でも 2 ETH までしか発注しない」という制限をかける
        # 資産規模に合わせて調整してください
        MAX_TRADE_SIZE = 2.0

        if sz == 0:
            return

        if sz > MAX_TRADE_SIZE:
            log.info(
                f"\n🛑 危険: 発注サイズ({sz} ETH)が上限({MAX_TRADE_SIZE} ETH)を超えています！"
            )
            log.info("計算ロジックを確認してください。Botを停止します。")
            sendDiscord("計算ロジックを確認してください。Botを停止します。")
            exit()  # プログラムを強制終了

        log.info(f"🚀 ORDER: {'BUY' if is_buy else 'SELL'} {sz} ETH")
        sendDiscord(f"🚀 ORDER: {'BUY' if is_buy else 'SELL'} {sz} ETH")
        try:
            # Hyperliquid SDKの成行注文関数
            market_result = self.exchange.market_open(self.coin, is_buy, sz=sz)
            print(f"✅ 成行注文完了: {market_result['status']}")
            sendDiscord(f"✅ 成行注文完了: {market_result['status']}")
            return "MAKER_FILLED"

        except Exception as e:
            print(f"❌ 成行注文も失敗しました (致命的エラー): {e}")
            sendDiscord(f"❌ 成行注文も失敗しました (致命的エラー): {e}")
            return "FAILED"
        # try:
        # # name="ETH" に修正済み
        # resp = self.exchange.market_open(
        #     name="ETH", is_buy=is_buy, sz=sz, px=None, slippage=0.01
        # )
        # # レスポンスの中身を簡易表示
        # status = resp["status"] if "status" in resp else resp
        # log.info(f"   Response: {status}")
        # manager.execute_smart_hedge(
        #     size=sz,
        #     panic_size=panic_amount_eth,
        #     is_buy=is_buy,
        #     calcRawDelta=self.calcRawDelta,
        #     panic_threshold=2 * self.ETHthreshold,
        #     max_retries=MAX_RETRY,
        #     wait_seconds=30,
        # )

        # except Exception as e:
        #     log.info(f"   Failed: {e}")

    # def cleanup_orders(self):
    #     try:
    #         open_orders = self.info.open_orders(MAIN_ACCOUNT_ADDRESS)
    #         if len(open_orders) == 0:  # 注文がなければ何もしない
    #             return
    #         log.info("Delta threshold met, cleaning up open orders...")
    #         sendDiscord("Delta threshold met, cleaning up open orders...")
    #         for order in open_orders:
    #             self.exchange.cancel("ETH", order["oid"])
    #             log.info(f"canceled order {order['oid']}")
    #         log.info("all open orders cleaned up. \n")
    #         sendDiscord("all open orders cleaned up. \n")
    #     except Exception as e:
    #         log.info(f"Order cleanup error: {e} \n")
    #         sendDiscord(f"Order cleanup error: {e} \n")

    def save_to_dynamodb(self, equity_data):
        """資産データをDynamoDBに送信"""
        try:
            # DynamoDBはfloatを受け付けないので Decimal に変換する処理
            # JSON形式の辞書を丸ごと変換します
            item = {
                "timestamp": equity_data["timestamp"],
                "uni_value": format_decimal(equity_data["uni_value"]),
                "hl_value": format_decimal(equity_data["hl_value"]),
                "funding_fees": format_decimal(equity_data["funding_fees"]),
                "step_pnl": format_decimal(equity_data["step_pnl"]),
                "cum_pnl": format_decimal(equity_data["cum_pnl"]),
                "total_equity": format_decimal(equity_data["total_equity"]),
                "eth_price": format_decimal(equity_data["eth_price"]),
                # 存在しない場合0デフォルトもこの関数なら安全
                "lp_delta": format_decimal(equity_data.get("lp_delta", 0)),
                "net_delta": format_decimal(equity_data.get("net_delta", 0)),
                "raw_net_delta": format_decimal(equity_data.get("raw_net_delta", 0)),
            }

            # 送信！
            self.table.put_item(Item=item)
            log.info(f"☁️ Saved to DynamoDB: ${equity_data['total_equity']:.2f}")
            sendDiscordReport(equity_data)

        except Exception as e:
            log.info(f"❌ DynamoDB Error: {e}")
            sendDiscord(f"❌ DynamoDB Error: {e}")

    def run(self):
        log.info("🛡️ Safe Bot Started. Waiting for liquidity...")
        last_log_time = datetime.datetime.now()
        # 定数定義
        DECIMALS_ETH = 1e18
        oorThreshold = 0.06

        # リポジションを行うクラスのインスタンス作成
        pr = PoolRepositioner(TARGET_TOKEN_ID, ARB_SECRET)

        if self.currentTokenId == 0:
            tryCount = 1
            while tryCount < 4:
                # TODO:プールのリポジションを実行
                # TODO: get_token_amountsは流動性Lから枚数を計算しているのでノーポジションからの作成の際には使用できない
                # そのためwalletから直接取得した枚数と、hyperliquidから取得した価格を渡す必要がある
                wethAmount, usdcAmount = self.getWalletWethAndUsdc()
                currentPrice = self.get_cex_price()
                response = pr.executeReposition(
                    RPC_URL, currentPrice, wethAmount, usdcAmount, False
                )

                if response:
                    log.info("Successfully minted position!!")

                    # リポジションクラスから新しいTOKENIDを取得してセット
                    newTokenId = self.get_latest_token_id()
                    pr.TokenID = newTokenId
                    log.info(f"setting new TokenID: {newTokenId}")
                    self.currentTokenId = newTokenId

                    break

                else:
                    log.info("FAILURE.... ")
                    tryCount += 1
                    if tryCount < 4:
                        log.info(f"Making a new challenge. TryCount: {tryCount} \n")
                        continue
                    else:
                        log.info("failed for 3times. Stopping bot ....")
                        sendDiscord("failed for 3times. Stopping bot ....")
                        exit()

        dataInit = self.get_onchain_data()

        sqrtPa = 1.0001 ** (dataInit["tickLower"] / 2)
        sqrtPb = 1.0001 ** (dataInit["tickUpper"] / 2)
        # レンジアウトスコアクラスのインスタンス生成
        oor = oorDetector(
            upperPrice=get_price_from_sqrt(sqrtPb),
            lowerPrice=get_price_from_sqrt(sqrtPa),
            thresholdScore=oorThreshold,
            k=0.9,
        )

        lpf = LowPassFilter(alpha=0.15)

        emaPrice = LowPassFilter(alpha=0.016)

        # v6_3追加
        tracker = DeltaPnLTracker()

        while True:
            data = self.get_onchain_data()

            # データ取得失敗時などはスキップ
            if data is None:
                time.sleep(3)
                continue

            # --- 1. ガード条件: 流動性が0なら何もしない ---
            if data["L"] == 0:
                log.info(
                    f"⏳ Position ID {TARGET_TOKEN_ID} has 0 Liquidity. Waiting..."
                )
                time.sleep(10)
                continue

            # --- 2. デルタ計算 (流動性がある場合) ---

            # Tick -> SqrtPrice (1.0001^(tick/2))
            sqrtPa = 1.0001 ** (data["tickLower"] / 2)
            sqrtPb = 1.0001 ** (data["tickUpper"] / 2)

            # 現在価格 (Raw)
            sp = data["sqrtP_raw"]
            L = data["L"]

            # LPFで平滑化した価格を計算
            smoothedSP = lpf.update(sp)

            # # トレンド判断用にemaを記録
            # ePrice = emaPrice.update(data["price"])

            # Uniswap V3 公式式によるETH保有量計算 (Wei単位)
            amount0_wei = 0.0

            if sp < sqrtPa:
                # 現在価格 < レンジ (全額ETH)
                # amount0 = L * (1/√Pa - 1/√Pb)
                amount0_wei = L * (1 / sqrtPa - 1 / sqrtPb)
                raw_amount0_wei = amount0_wei

            elif sp >= sqrtPb:
                # 現在価格 > レンジ (全額USDC, ETHは0)
                amount0_wei = 0.0
                raw_amount0_wei = amount0_wei
            else:
                # レンジ内 (混合)
                # amount0 = L * (1/√P - 1/√Pb)
                amount0_wei = L * (1 / smoothedSP - 1 / sqrtPb)
                raw_amount0_wei = L * (1 / sp - 1 / sqrtPb)

            # Wei -> ETH (10^18) に変換
            lp_delta_eth = amount0_wei / DECIMALS_ETH
            raw_lp_delra_eth = raw_amount0_wei / DECIMALS_ETH

            # 追加:walletのeth残高もヘッジする
            walletEth = self.getWalletEth()

            # ネットデルタ (LPのETH + ヘッジのETH + walletのETH)
            net_delta = lp_delta_eth + data["hedge_pos"] + walletEth
            raw_net_delta = raw_lp_delra_eth + data["hedge_pos"] + walletEth

            # --- 表示用データの作成 ---
            current_price = data["price"]
            lp_value_usd = lp_delta_eth * current_price  # ETH枚数 * ドル価格

            log.info(
                f"\r Price:${current_price:.1f} | CurrentThreshold:{self.ETHthreshold:.3f} | LP:{lp_delta_eth:.3f}ETH (${lp_value_usd:.0f}) | Hedge:{data['hedge_pos']:.3f} | Net:{net_delta:.4f} \n"
            )

            # 6_4 トレード済みの場合Trueとするフラグ、各ループ判定前にfalseで初期化
            hasAlreadyTraded = False

            # クールタイム判定用の時刻を取得
            currentTime = time.time()

            if abs(net_delta) > self.ETHthreshold:
                elapsedTime = currentTime - self.cooltime

                if self.firstBreachTime is None:
                    self.firstBreachTime = datetime.datetime.now()
                elif (datetime.datetime.now() - self.firstBreachTime).seconds > 5:
                    if elapsedTime <= 300:
                        # クールタイムが５分以下の時はまだ取引しない
                        log.info("\n has traded in 5 minutes. Cooldowning...")
                        sendDiscord("\n has traded in 5 minutes. Cooldowning...")
                    else:
                        log.info(
                            f"\n🚨 Rebalance Required! Net Delta: {net_delta:.4f}, Current Price: {current_price:.2f}"
                        )
                        orderLog.info(
                            f"\n🚨 Rebalance Required! Net Delta: {net_delta:.4f}, Current Price: {current_price:.2f}"
                        )
                        sendDiscord(
                            f"\n🚨 Rebalance Required! Net Delta: {net_delta:.4f}, Current Price: {current_price:.2f}"
                        )
                        # ネットデルタを打ち消す
                        self.execute_trade(-1 * net_delta)
                        lpf.smoothed_value = None

                        # トレード実行後にフラグをTrueに
                        hasAlreadyTraded = True
                        # クールタイムに取引時刻を記録
                        self.cooltime = currentTime

            else:
                self.firstBreachTime = None  # 範囲内に戻ったらリセット
                # if net_delta > 0:
                #     target_delta = self.ETHthreshold * TARGET_RATIO
                # else:
                #     target_delta = -1 * (self.ETHthreshold * TARGET_RATIO)

                # hedgeSize = -1 * (net_delta - target_delta)
                # self.execute_trade(hedgeSize, manager=manager)
                # time.sleep(5)  # 注文後の待機

            # v6_2 緊急脱出処理
            if (
                abs(raw_net_delta) < 3.3 * self.ETHthreshold
                and abs(raw_net_delta) > 1.5 * self.ETHthreshold
                and (not hasAlreadyTraded)
            ):
                if self.BailoutBreachTime is None:
                    self.BailoutBreachTime = datetime.datetime.now()

                elif (datetime.datetime.now() - self.BailoutBreachTime).seconds > 2:
                    log.info(
                        f"\n🚨BAILOUT!! Rebalance Required! Raw Net Delta: {raw_net_delta:.4f}, Current Price: {current_price:.2f}"
                    )
                    orderLog.info(
                        f"\n🚨BAILOUT!! Rebalance Required! Raw Net Delta: {raw_net_delta:.4f}, Current Price: {current_price:.2f}"
                    )
                    sendDiscord(
                        f"\n🚨BAILOUT!! Rebalance Required! Raw Net Delta: {raw_net_delta:.4f}, Current Price: {current_price:.2f}"
                    )

                    sz = -1 * raw_net_delta

                    # ネットデルタを打ち消す
                    self.execute_trade(sz)

                    # クールタイムを記録
                    self.cooltime = currentTime

                    lpf.smoothed_value = None

            else:
                self.BailoutBreachTime = None

            # ver2追加　OutOfRangeスコアの計算、uniswapプールでのリポジションを行う
            if oor.runDetector(currentPrice=current_price):
                sendDiscord("went out of range, making new position")
                tryCount = 1

                while tryCount < 4:
                    # TODO:プールのリポジションを実行
                    tokenAmounts = self.get_token_amounts(
                        data["L"],
                        data["sqrtP_raw"],
                        data["tickLower"],
                        data["tickUpper"],
                    )
                    response = pr.executeReposition(
                        RPC_URL, current_price, tokenAmounts[0], tokenAmounts[1], False
                    )

                    if response:
                        log.info("Successfully minted position!!")

                        # リポジションクラスから新しいTOKENIDを取得してセット
                        newTokenId = self.get_latest_token_id()
                        pr.TokenID = newTokenId
                        log.info(f"setting new TokenID: {newTokenId}")
                        self.currentTokenId = newTokenId
                        break

                    else:
                        log.info("FAILURE.... ")
                        tryCount += 1
                        if tryCount < 4:
                            log.info(f"Making a new challenge. TryCount: {tryCount} \n")
                        else:
                            log.info("failed for 3times. Stopping bot ....")
                            sendDiscord("failed for 3times. Stopping bot ....")
                            exit()

            # dynamoDB壁録
            now = datetime.datetime.now()
            if (now - last_log_time).total_seconds() > RECORD_TIME:
                equity = self.get_total_equity()
                if equity:
                    # v6_3 add
                    step_pnl, cum_pnl = tracker.update(current_price, raw_net_delta)

                    # ★追加: デルタ情報を辞書に追加
                    equity["price_ema"] = emaPrice.smoothed_value
                    equity["lp_delta"] = lp_delta_eth
                    equity["net_delta"] = net_delta
                    equity["raw_net_delta"] = raw_net_delta
                    equity["step_pnl"] = step_pnl
                    equity["cum_pnl"] = cum_pnl
                    self.save_to_dynamodb(equity)
                    last_log_time = now
            time.sleep(20)

            # この時点で残っている注文は削除する
            # self.cleanup_orders()


if __name__ == "__main__":
    bot = SafeRealBot()
    # スレッショルドの初期値を与えておく
    bot.ETHthreshold = THRESHOLD
    bot.run()
