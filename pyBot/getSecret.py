import os

import boto3
from dotenv import load_dotenv

load_dotenv("./.env")


def get_secret_key():
    # クライアントの作成（aws configureの設定が自動で使われます）
    ssm = boto3.client("ssm", region_name="ap-northeast-1")

    try:
        response = ssm.get_parameter(
            Name=os.environ.get("AWS_SSM"),  # 手順1で決めた名前
            WithDecryption=True,  # SecureStringを復号化して取得
        )
        return response["Parameter"]["Value"].strip()
    except Exception as e:
        print(f"Error fetching parameter: {e}")
        raise e
