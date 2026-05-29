"""推送通知模块，支持 pushplus / telegram / wxpusher / serverchan。

环境变量:
  PUSH_METHOD     - 推送渠道: pushplus / telegram / wxpusher / serverchan
  PUSHPLUS_TOKEN  - pushplus 的 token
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID - telegram 配置
  WXPUSHER_SPT    - wxpusher 的 SPT
  SERVERCHAN_SPT  - Server 酱的 SendKey
"""

import json
import logging
import os
import random
import time
import warnings

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .*doesn\x27t match a supported version.*",
    category=Warning,
)

import requests

logger = logging.getLogger("push")


class Push:
    def __init__(self):
        self.headers = {"Content-Type": "application/json"}

    def pushplus(self, title, content, token):
        for attempt in range(5):
            try:
                resp = requests.post(
                    "https://www.pushplus.plus/send",
                    data=json.dumps({"token": token, "title": title, "content": content}).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                resp.raise_for_status()
                logger.info("PushPlus 推送成功: %s", resp.text)
                return True
            except requests.RequestException as e:
                logger.error("PushPlus 失败 (attempt %d): %s", attempt + 1, e)
                if attempt < 4:
                    time.sleep(random.randint(30, 60))
        return False

    def telegram(self, content, bot_token, chat_id):
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            resp = requests.post(url, json={"chat_id": chat_id, "text": content}, timeout=30)
            resp.raise_for_status()
            logger.info("Telegram 推送成功")
            return True
        except requests.RequestException as e:
            logger.error("Telegram 推送失败: %s", e)
            return False

    def wxpusher(self, content, spt):
        url = f"https://wxpusher.zjiecode.com/api/send/message/{spt}/{content}"
        for attempt in range(5):
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                logger.info("WxPusher 推送成功: %s", resp.text)
                return True
            except requests.RequestException as e:
                logger.error("WxPusher 失败 (attempt %d): %s", attempt + 1, e)
                if attempt < 4:
                    time.sleep(random.randint(30, 60))
        return False

    def serverchan(self, title, content, spt):
        url = f"https://sctapi.ftqq.com/{spt}.send"
        for attempt in range(5):
            try:
                resp = requests.post(
                    url,
                    data=json.dumps({"title": title, "desp": content}).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                resp.raise_for_status()
                logger.info("ServerChan 推送成功: %s", resp.text)
                return True
            except requests.RequestException as e:
                logger.error("ServerChan 失败 (attempt %d): %s", attempt + 1, e)
                if attempt < 4:
                    time.sleep(random.randint(30, 60))
        return False


def send(title: str, content: str, is_success: bool = True) -> bool:
    """统一推送入口，根据 PUSH_METHOD 环境变量选择渠道。"""
    method = os.getenv("PUSH_METHOD", "")
    if not method:
        logger.info("未配置 PUSH_METHOD，跳过推送")
        return False

    push = Push()
    method = method.lower()

    if method == "pushplus":
        token = os.getenv("PUSHPLUS_TOKEN", "")
        if not token:
            logger.warning("PUSHPLUS_TOKEN 未配置")
            return False
        return push.pushplus(title, content, token)

    if method == "telegram":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not bot_token or not chat_id:
            logger.warning("TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置")
            return False
        return push.telegram(content, bot_token, chat_id)

    if method == "wxpusher":
        spt = os.getenv("WXPUSHER_SPT", "")
        if not spt:
            logger.warning("WXPUSHER_SPT 未配置")
            return False
        return push.wxpusher(content, spt)

    if method == "serverchan":
        spt = os.getenv("SERVERCHAN_SPT", "")
        if not spt:
            logger.warning("SERVERCHAN_SPT 未配置")
            return False
        return push.serverchan(title, content, spt)

    logger.warning("不支持的 PUSH_METHOD: %s，支持: pushplus, telegram, wxpusher, serverchan", method)
    return False
