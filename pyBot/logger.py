import logging
import sys
from logging.handlers import RotatingFileHandler


def setup_logger(name="BotLogger", log_file="bot_log.log", level=logging.INFO):
    """
    コンソールとファイルの両方に出力するロガーを作成する関数

    Args:
        name (str): ロガーの名前
        log_file (str): ログファイル名
        level (int): ログレベル (logging.INFO, logging.DEBUG 等)
    """
    # 1. ロガーの作成
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 重複してハンドラが追加されるのを防ぐ（リロード時対策）
    if logger.hasHandlers():
        return logger

    # 2. フォーマットの設定（日付 - レベル - メッセージ）
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 3. ファイルハンドラ（ファイルへの出力）
    # RotatingFileHandler: ファイルが大きくなったら分割する
    # maxBytes=5MB, backupCount=5 (5MB x 5ファイルまで保持)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    # 4. ストリームハンドラ（コンソール画面への出力）
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    # 5. ロガーにハンドラを追加
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger
