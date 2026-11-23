#!/usr/bin/env python3
"""Bing Rewards 每日任务自动化 — 多账号版。

通过 GitHub Actions 定时运行，从 BING_ACCOUNTS 环境变量读取多账号配置。

主方案（cookies 字段，F12 DevTools 复制）：
  {"name": "主号", "cookies": "MUID=xxx; _U=yyy; ...", "appkey": ""}

备用方案（session 字段，login_helper.py 生成，优先级更高）：
  {"name": "主号", "session": {...}, "appkey": ""}
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as url_encode, urlsplit

import httpx
from playwright.async_api import (
    async_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
)

from browser_utils import (
    BING_URL, REWARDS_URL, HEADLESS_ARGS, XVFB_ARGS, DEFAULT_CONTEXT_OPTIONS,
    ANTI_DETECTION_SCRIPT, USER_AGENT,
    check_logged_in, get_rewards_points,
)
from push import send as push_send

# ═══════════════════════════════════════════════════════════
# 配置项（可根据需要修改）
# ═══════════════════════════════════════════════════════════

# 每个账号执行的搜索次数（参照原脚本 25 次上限）
MAX_SEARCHES = 30

# 两次搜索之间的基础随机延迟范围（秒），参照原脚本 20-80 秒
MIN_SEARCH_DELAY = 20
MAX_SEARCH_DELAY = 80

# 每 4 次搜索后额外等待（秒），模拟原脚本 PAUSE_TIME 机制
PAUSE_EVERY_N = 4
PAUSE_DURATION = 30

# 两个每日活动点击之间的间隔（秒）
ACTIVITY_CLICK_DELAY = 2

# 页面加载超时（毫秒）
PAGE_TIMEOUT = 30000

# 两个账号之间的冷却时间（秒），避免同 IP 连续操作被风控
ACCOUNT_COOLDOWN = 300

# 持久化浏览器 profile 目录（服务器上使用 launch_persistent_context）
PROFILES_DIR = os.path.join(os.path.expanduser("~"), ".bing-rewards-profiles")

# 热门词 API 地址（原脚本来源：api.gmya.net）
HOT_WORDS_API = "https://api.gmya.net/Api/"

# 热门词来源池，每次随机选一个
KEYWORD_SOURCES = ["ZhiHuHot", "WeiBoHot", "TouTiaoHot", "DouYinHot", "BaiduHot"]

# 默认搜索词库（同原脚本，中文古诗词/谚语）
DEFAULT_SEARCH_WORDS = [
    "盛年不重来，一日难再晨", "千里之行，始于足下",
    "少年易学老难成，一寸光阴不可轻", "敏而好学，不耻下问",
    "海内存知已，天涯若比邻", "三人行，必有我师焉",
    "莫愁前路无知已，天下谁人不识君", "人生贵相知，何用金与钱",
    "天生我材必有用", "海纳百川有容乃大",
    "读书破万卷，下笔如有神", "学而不思则罔，思而不学则殆",
    "莫等闲，白了少年头，空悲切", "少壮不努力，老大徒伤悲",
    "一寸光阴一寸金，寸金难买寸光阴", "近朱者赤，近墨者黑",
    "吾生也有涯，而知也无涯", "纸上得来终觉浅，绝知此事要躬行",
    "己所不欲，勿施于人", "天将降大任于斯人也",
    "鞠躬尽瘁，死而后已", "书到用时方恨少",
    "天下兴亡，匹夫有责", "人无远虑，必有近忧",
    "为中华之崛起而读书", "一日无书，百事荒废",
    "岂能尽如人意，但求无愧我心", "人生自古谁无死，留取丹心照汗青",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
# Windows GBK 终端下切换 stdout 到 UTF-8，避免 emoji/中文编码报错
if sys.platform == "win32":
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
# Windows GBK 终端下替换 emoji 避免编码报错
if sys.platform == "win32":
    for handler in logging.root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1))
log = logging.getLogger("bing_rewards")


def load_accounts():
    """从环境变量 BING_ACCOUNTS 或 BING_ACCOUNTS_FILE 加载多账号配置。"""
    raw = os.getenv("BING_ACCOUNTS", "")
    file_path = os.getenv("BING_ACCOUNTS_FILE", "")
    if not raw and file_path:
        try:
            raw = open(file_path, "r", encoding="utf-8").read().strip()
        except Exception as e:
            log.error("读取 %s 失败: %s", file_path, e)
            return []
    if not raw:
        log.error("BING_ACCOUNTS 或 BING_ACCOUNTS_FILE 均未配置")
        return []
    try:
        accounts = json.loads(raw)
        if not isinstance(accounts, list):
            log.error("BING_ACCOUNTS 格式错误：需要 JSON 数组")
            return []
        log.info("加载了 %d 个账号", len(accounts))
        return accounts
    except json.JSONDecodeError as e:
        log.error("BING_ACCOUNTS JSON 解析失败: %s", e)
        return []


def parse_cookies(raw: str):
    """将 DevTools 复制的 Cookie 字符串解析为 Playwright 格式。"""
    cookies = []
    for item in raw.split(";"):
        item = item.strip()
        if "=" in item:
            name, value = item.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".bing.com",
                "path": "/",
            })
    return cookies


async def fetch_hot_words(appkey: str = ""):
    """获取热门搜索词，appkey 为空时也能正常使用。"""
    source = random.choice(KEYWORD_SOURCES)
    url = f"{HOT_WORDS_API}{source}?format=json"
    if appkey:
        url += f"&appkey={appkey}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            data = resp.json()
            if data.get("data") and any(data["data"]):
                words = [item["title"] for item in data["data"] if item.get("title")]
                log.info("热门词获取成功 (来源=%s, 数量=%d)", source, len(words))
                return words
    except Exception as e:
        log.warning("热门词获取失败 (%s)，使用默认词库", e)
    return DEFAULT_SEARCH_WORDS


# 积分日的业务时区未验证。检查点同时记下调用方日期和 Asia/Shanghai 日期，
# 任一日期变化都按可能重置处理，而且检查点本身不能当成完成。
SHANGHAI_TZ = timezone(timedelta(hours=8))
CHECKPOINT_FILENAME = "resume-checkpoint.json"
MAX_PAGE_REOPEN = 1
PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")
_CLOSED_TARGET_MARKER = "target page, context or browser has been closed"
_RESTRICTION_URL_MARKERS = (
    "captcha",
    "/challenge",
    "account.live.com/abuse",
    "account.live.com/identity/confirm",
    "account.live.com/recover",
    "account.live.com/proofs",
)
_RESTRICTION_TEXT_MARKERS = (
    "verify you are human",
    "unusual activity",
    "account has been suspended",
    "account suspended",
    "suspended account",
    "账号已被暂停",
    "账号已被限制",
    "账号被封禁",
)


class BrowserClosedError(RuntimeError):
    def __init__(self, completed=0):
        super().__init__("browser context closed")
        self.completed = int(completed)


class AccountRestrictedError(RuntimeError):
    def __init__(self):
        super().__init__("restricted")


def is_target_closed_error(exc) -> bool:
    return isinstance(exc, PlaywrightError) and _CLOSED_TARGET_MARKER in str(exc).casefold()


def context_is_usable(context) -> bool:
    if context is None:
        return False
    try:
        is_closed = getattr(context, "is_closed", None)
        if callable(is_closed) and is_closed():
            return False
        browser = getattr(context, "browser", None)
        if callable(browser):
            browser = browser()
        if browser is not None:
            connected = getattr(browser, "is_connected", None)
            if callable(connected) and not connected():
                return False
    except Exception:
        return False
    return True


def page_is_closed(page) -> bool:
    try:
        is_closed = getattr(page, "is_closed", None)
        return bool(is_closed()) if callable(is_closed) else False
    except Exception:
        return True


async def restriction_stop_reason(page):
    """识别验证码、封禁或账号限制。只决定停止，不包含绕过。"""
    url = str(getattr(page, "url", "") or "").casefold()
    if any(marker in url for marker in _RESTRICTION_URL_MARKERS):
        return "url"
    title_fn = getattr(page, "title", None)
    title = ""
    if callable(title_fn):
        try:
            title = str(await title_fn() or "")
        except Exception:
            title = ""
    folded = title.casefold()
    if any(marker.casefold() in folded for marker in _RESTRICTION_TEXT_MARKERS):
        return "title"
    return None


async def stop_if_restricted(page):
    if await restriction_stop_reason(page):
        log.error("检测到验证码或账号限制，立即停止（不绕过、不更换身份或代理）")
        raise AccountRestrictedError()


def rewards_day_keys(now: datetime) -> dict:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("rewards day comparison requires an aware timestamp")
    return {
        "local_date": now.date().isoformat(),
        "shanghai_date": now.astimezone(SHANGHAI_TZ).date().isoformat(),
        "rewards_timezone": "unverified",
    }


def checkpoint_period_relation(record, now: datetime) -> str:
    if not record:
        return "absent"
    keys = record.get("day_keys") if isinstance(record, dict) else None
    if not isinstance(keys, dict):
        return "unknown"
    if keys.get("rewards_timezone") != "unverified":
        return "unknown"
    if "local_date" not in keys or "shanghai_date" not in keys:
        return "unknown"
    current = rewards_day_keys(now)
    if (
        keys.get("local_date") != current["local_date"]
        or keys.get("shanghai_date") != current["shanghai_date"]
    ):
        return "reset"
    return "same_calendar_dates_timezone_unverified"


def _snapshot_ints(snapshot):
    if not isinstance(snapshot, dict):
        return None
    try:
        parsed = {
            "search_earned": int(snapshot["search_earned"]),
            "search_max": int(snapshot["search_max"]),
            "activities_done": int(snapshot["activities_done"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if parsed["search_earned"] < 0 or parsed["activities_done"] < 0 or parsed["search_max"] <= 0:
        return None
    return parsed


def _exact_int(value):
    """只接受整数或纯数字字符串。拒绝 bool、float 和截断。"""
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return int(value.strip())
    return None


def _labeled_int(text, label):
    if not text:
        return None
    match = re.search(
        rf"(?i)(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_])\s*[:=]\s*([0-9]+)",
        text,
    )
    if not match:
        return None
    return int(match.group(1))


def proven_search_remaining(snapshot, max_searches=MAX_SEARCHES):
    """返回还能做的搜索次数。无法证明时返回 None，调用方必须停止。

    页面上的「必应搜索 X/Y」是累计分数，不是次数。只有同时存在
    pointProgress、pointProgressMax 和 pointsPerSearch，且剩余分数能被
    每次上限整除、并与累计分数一致时，才把商当作次数。已满既有完成线
    或已满显示上限时返回 0。历史尝试次数和分数缺口都不能换成次数。
    max_searches 只是配置硬限，不参与换算。
    """
    del max_searches
    if not isinstance(snapshot, dict):
        return None
    parsed = _snapshot_ints(snapshot)
    progress = _exact_int(snapshot.get("pointProgress"))
    progress_max = _exact_int(snapshot.get("pointProgressMax"))
    per_action = _exact_int(snapshot.get("pointsPerSearch"))
    has_any_progress = any(key in snapshot for key in ("pointProgress", "pointProgressMax", "pointsPerSearch"))
    if has_any_progress and (progress is None or progress_max is None or per_action is None):
        return None
    if progress is not None:
        if per_action <= 0 or progress < 0 or progress_max < progress:
            return None
        if parsed and (parsed["search_earned"] != progress or parsed["search_max"] != progress_max):
            return None
        remaining_points = progress_max - progress
        if remaining_points % per_action != 0:
            return None
        return remaining_points // per_action
    if not parsed:
        return None
    if parsed["search_earned"] >= 60 or parsed["search_earned"] >= parsed["search_max"]:
        return 0
    return None


def search_step_status(before, after):
    """比较一次搜索前后的实读进度。无法证明刚好前进一次就不要继续。"""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "unknown"
    after_parsed = _snapshot_ints(after)
    if after_parsed and rewards_quotas_complete(after_parsed):
        earned_before = _exact_int(before.get("search_earned"))
        earned_after = after_parsed["search_earned"]
        if earned_before is not None and earned_after < earned_before:
            return "inconsistent"
        return "complete"
    before_left = proven_search_remaining(before)
    after_left = proven_search_remaining(after)
    if before_left is None or after_left is None:
        return "unknown"
    if after_left == before_left:
        before_progress = _exact_int(before.get("pointProgress"))
        after_progress = _exact_int(after.get("pointProgress"))
        if before_progress is not None and before_progress == after_progress:
            return "unchanged"
        if before_progress is None and _exact_int(before.get("search_earned")) == _exact_int(after.get("search_earned")):
            return "unchanged"
        return "inconsistent"
    if after_left != before_left - 1:
        return "inconsistent"
    per_action = _exact_int(before.get("pointsPerSearch"))
    if (
        per_action is None
        or per_action != _exact_int(after.get("pointsPerSearch"))
        or _exact_int(before.get("pointProgressMax")) != _exact_int(after.get("pointProgressMax"))
        or _exact_int(after.get("pointProgress")) != _exact_int(before.get("pointProgress")) + per_action
    ):
        return "inconsistent"
    if after_left == 0:
        return "complete"
    return "advanced"


def remaining_activities(snapshot):
    parsed = _snapshot_ints(snapshot)
    if not parsed:
        return None
    return max(0, min(3, 3 - parsed["activities_done"]))


def plan_remaining_work(live_snapshot, checkpoint_record, now):
    relation = checkpoint_period_relation(checkpoint_record, now)
    parsed = _snapshot_ints(live_snapshot)
    if parsed and rewards_quotas_complete(parsed):
        return {
            "action": "skip",
            "reason": "live_quotas_complete",
            "checkpoint_relation": relation,
            "complete": True,
            "searches": 0,
            "activities": 0,
        }
    searches = proven_search_remaining(live_snapshot)
    activities = remaining_activities(live_snapshot)
    if searches is None or activities is None:
        return {
            "action": "stop",
            "reason": "live_credit_unknown",
            "checkpoint_relation": relation,
            "complete": False,
            "searches": None,
            "activities": None,
        }
    if searches == 0 and activities == 0:
        return {
            "action": "stop",
            "reason": "incomplete_without_proven_work",
            "checkpoint_relation": relation,
            "complete": False,
            "searches": 0,
            "activities": 0,
        }
    return {
        "action": "run",
        "reason": "live_remaining",
        "checkpoint_relation": relation,
        "complete": False,
        "searches": searches,
        "activities": activities,
    }


async def run_refresh_bounded_searches(refresh, search_once, max_searches=MAX_SEARCHES):
    """每次搜索前后重新读取剩余次数。无法证明、没变化或不一致就停止。"""
    try:
        cap = int(max_searches)
    except (TypeError, ValueError):
        cap = 0
    cap = max(0, min(cap, MAX_SEARCHES))
    completed = 0
    while completed < cap:
        before = await refresh()
        remaining = proven_search_remaining(before)
        if remaining == 0:
            return {"action": "done", "completed": completed, "reason": "remaining_zero", "complete": False}
        if remaining is None:
            return {"action": "stop", "completed": completed, "reason": "remaining_unproven", "complete": False}
        await search_once()
        completed += 1
        after = await refresh()
        status = search_step_status(before, after)
        if status == "complete":
            return {"action": "done", "completed": completed, "reason": "refreshed_complete", "complete": False}
        if status != "advanced":
            return {"action": "stop", "completed": completed, "reason": status, "complete": False}
    final = await refresh()
    if proven_search_remaining(final) == 0:
        return {"action": "done", "completed": completed, "reason": "hard_cap_complete", "complete": False}
    return {"action": "stop", "completed": completed, "reason": "hard_cap", "complete": False}


def account_checkpoint_key(name: str) -> str:
    return hashlib.sha256(str(name).encode("utf-8")).hexdigest()


def _public_snapshot(snapshot):
    parsed = _snapshot_ints(snapshot)
    if not parsed:
        return None
    if isinstance(snapshot, dict) and "today_total" in snapshot:
        try:
            parsed["today_total"] = int(snapshot["today_total"])
        except (TypeError, ValueError):
            pass
    return parsed


def record_checkpoint(account_name, *, now, directory, reason, complete, snapshot, searches_started):
    public = _public_snapshot(snapshot)
    entry = {
        "complete": bool(complete and public and rewards_quotas_complete(public)),
        "reason": str(reason),
        "searches_started": max(0, int(searches_started or 0)),
        "day_keys": rewards_day_keys(now),
        "snapshot": public,
    }
    directory = directory or PROFILES_DIR
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, CHECKPOINT_FILENAME)
    data = {"version": 1, "accounts": {}}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and isinstance(loaded.get("accounts"), dict):
                data["accounts"] = loaded["accounts"]
        except (OSError, json.JSONDecodeError):
            data = {"version": 1, "accounts": {}}
    data["accounts"][account_checkpoint_key(account_name)] = entry
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if json.dumps(str(account_name), ensure_ascii=False) in text:
        raise RuntimeError("checkpoint refused")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.write("\n")
    os.replace(temporary, path)
    return path


def load_checkpoint_record(account_name, directory=None):
    path = os.path.join(directory or PROFILES_DIR, CHECKPOINT_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        return None
    record = accounts.get(account_checkpoint_key(account_name))
    return record if isinstance(record, dict) else None


def remember_checkpoint(account_name, **kwargs):
    try:
        record_checkpoint(account_name, **kwargs)
    except Exception:
        log.error("检查点未写入")


def pid_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_from_lock_target(target: str):
    tail = str(target or "").rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return None


def inspect_profile_lock(profile_dir, pid_alive=None):
    """识别 profile 锁归属。不删除任何锁文件。"""
    alive = pid_alive or pid_is_alive
    paths = [
        os.path.join(profile_dir, name)
        for name in PROFILE_LOCK_NAMES
        if os.path.lexists(os.path.join(profile_dir, name))
    ]
    if not paths:
        return {"state": "absent", "locks": 0}
    parsed = False
    live = False
    for path in paths:
        target = ""
        if os.path.islink(path):
            try:
                target = os.readlink(path)
            except OSError:
                target = ""
        pid = _pid_from_lock_target(target)
        if pid is None:
            continue
        parsed = True
        if alive(pid):
            live = True
    if live:
        state = "live"
    elif parsed:
        state = "stale"
    else:
        state = "unknown"
    return {"state": state, "locks": len(paths)}


def profile_launch_allowed(lock_info) -> bool:
    return isinstance(lock_info, dict) and lock_info.get("state") == "absent"


def should_send_notification(selective: bool) -> bool:
    return not selective


def emit_run_notification(selective, local, title, content, is_success) -> bool:
    if local or not should_send_notification(selective):
        log.info("本次不发送通知")
        return False
    return bool(push_send(title, content, is_success=is_success))


def filter_accounts_for_invocation(accounts, account_name=""):
    if not account_name:
        return list(accounts), None
    selected = [account for account in accounts if account.get("name") == account_name]
    if not selected:
        return [], "missing"
    if len(selected) != 1:
        return [], "duplicate"
    return selected, None


async def perform_searches(page, search_words, context, search_limit=None):
    """执行搜索 — 顺便贡献 Edge 浏览时长（点击链接模拟真实浏览）。"""
    CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    if search_limit is None:
        limit = MAX_SEARCHES
    else:
        limit = int(search_limit)
    if limit < 0:
        limit = 0
    if limit > MAX_SEARCHES:
        limit = MAX_SEARCHES
    log.info("开始 %d 次搜索", limit)
    for i in range(limit):
        word = search_words[i % len(search_words)]
        form_str = "".join(random.choices(CHARS, k=4))
        cvid_str = "".join(random.choices(CHARS, k=32))
        search_url = (
            f"https://cn.bing.com/search?q={url_encode(word)}"
            f"&form={form_str}&cvid={cvid_str}"
        )
        for recovery_attempt in range(MAX_PAGE_REOPEN + 1):
            try:
                # 先滚动到底部（模仿原脚本 smoothScrollToBottom 行为）
                await page.goto(BING_URL, wait_until="domcontentloaded", timeout=15000)
                await stop_if_restricted(page)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(random.uniform(0.5, 1.5))

                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                await stop_if_restricted(page)
                log.info("[%d/%d] %s", i + 1, limit, word[:30])

                # ── Edge 浏览：模拟真人浏览 ──
                await page.evaluate("window.scrollBy(0, 400)")
                await asyncio.sleep(0.5)
                await page.evaluate("window.scrollBy(0, 400)")
                await asyncio.sleep(0.5)
                await page.evaluate("window.scrollBy(0, -200)")
                await asyncio.sleep(0.5)
                # 每 2 次搜索点一个外部链接，在那等完剩余延迟
                if i % 2 == 0:
                    await asyncio.sleep(2)
                    try:
                        links = page.locator('a[href*="http"]')
                        for li in range(min(10, await links.count())):
                            link = links.nth(li)
                            if await link.is_visible():
                                href = (await link.get_attribute('href') or '')
                                if href and 'bing.com' not in href and 'microsoft.com' not in href:
                                    log.info("  ↪ %s", href[:60])
                                    await link.click()
                                    await asyncio.sleep(random.uniform(3, 6))
                                    break
                    except Exception as exc:
                        if isinstance(exc, (AccountRestrictedError, BrowserClosedError)) or is_target_closed_error(exc):
                            raise
            except PlaywrightTimeout:
                log.warning("[%d/%d] 搜索超时，跳过", i + 1, limit)
                break
            except PlaywrightError as exc:
                if not is_target_closed_error(exc):
                    raise
                if recovery_attempt or not context_is_usable(context):
                    raise BrowserClosedError(completed=i)
                log.warning("[%d/%d] 页面已关闭，有限重开一次后重试当前搜索", i + 1, limit)
                try:
                    page = await context.new_page()
                except PlaywrightError as reopen_exc:
                    if is_target_closed_error(reopen_exc) or not context_is_usable(context):
                        raise BrowserClosedError(completed=i)
                    raise
            else:
                break

        delay = random.uniform(MIN_SEARCH_DELAY, MAX_SEARCH_DELAY)
        # 每 PAUSE_EVERY_N 次搜索插入额外长暂停（原脚本 PAUSE_TIME 机制）
        if (i + 1) % PAUSE_EVERY_N == 0:
            delay += PAUSE_DURATION
        await asyncio.sleep(delay)
    log.info("搜索任务完成")
    return page


    """尝试在 quiz/poll 页面随机点击选项并确认。"""
    try:
        await asyncio.sleep(1)
        for sel in ['[role="option"]', 'input[type="radio"]', '.bt_poll', '.quizOption']:
            elements = page.locator(sel)
            count = await elements.count()
            if count > 0:
                await elements.nth(random.randint(0, count - 1)).click()
                log.info("quiz: 点击了选项 (%s)", sel)
                await asyncio.sleep(1)
                break
        for sel in ['button:has-text("Next")', 'button:has-text("Done")', 'button:has-text("Submit")']:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await asyncio.sleep(1)
    except Exception:
        pass


async def click_daily_activities(page, max_clicks=3):
    """点击 Rewards 每日活动中的点击搜索项（每个 10 分），返回成功数。"""
    try:
        max_clicks = int(max_clicks)
    except (TypeError, ValueError):
        max_clicks = 0
    max_clicks = max(0, min(3, max_clicks))
    if max_clicks == 0:
        log.info("每日活动额度已满，跳过")
        return 0
    log.info("进入 Rewards 页面，查找每日活动...")
    try:
        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    except PlaywrightTimeout:
        log.warning("Rewards 页面加载超时")
        await stop_if_restricted(page)
        return 0
    except PlaywrightError as exc:
        if is_target_closed_error(exc):
            raise BrowserClosedError(completed=0)
        raise
    await stop_if_restricted(page)
    await asyncio.sleep(5)

    # 直接用 JS 找所有可见的 bing 搜索链接并点击
    # Playwright 的 a[href="..."] 定位长 URL 时容易不匹配
    clicked = await page.evaluate("""
        async () => {
            const links = [];
            const seen = new Set();
            for (const a of document.querySelectorAll('a[href*="bing.com/search"]')) {
                const href = a.href;
                if (href.includes('rewards.bing.com')) continue;
                if (seen.has(href)) continue;
                seen.add(href);
                const r = a.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                links.push(a);
            }

            let done = 0;
            for (const el of links.slice(0, %%MAX_CLICKS%%)) {
                try {
                    el.click();
                    await new Promise(r => setTimeout(r, 3000));
                    done++;
                } catch(e) {
                    // ignore
                }
            }
            return done;
        }
    """.replace("%%MAX_CLICKS%%", str(max_clicks)))

    log.info("每日活动完成: 成功 %d/%d", clicked, max_clicks)
    return clicked



async def _open_points_details(page):
    """打开积分明细弹窗，常规点击失败时再强制点击或点父级可点击元素。"""
    details_link = page.get_by_text("积分明细", exact=True)
    if await details_link.count() == 0:
        details_link = page.get_by_text("Points breakdown", exact=True)
    if await details_link.count() == 0:
        raise ValueError("缺少每日活动进度或积分明细入口")

    target = details_link.last
    await target.wait_for(state="visible", timeout=15000)
    try:
        await target.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        await target.click(timeout=15000)
    except PlaywrightTimeout:
        log.warning("积分明细常规点击超时，改用强制点击")
        try:
            await target.click(timeout=8000, force=True)
        except PlaywrightTimeout:
            await target.evaluate(
                """el => {
                    el.click();
                    const clickable = el.closest('button, a, [role="button"], [role="link"]') || el.parentElement;
                    if (clickable && clickable !== el) clickable.click();
                }"""
            )
    await page.wait_for_timeout(1000)
    dialogs = page.locator('[role="dialog"]')
    if await dialogs.count() == 0:
        await target.evaluate(
            """el => {
                el.click();
                const clickable = el.closest('button, a, [role="button"], [role="link"]') || el.parentElement;
                if (clickable && clickable !== el) clickable.click();
            }"""
        )
        await page.wait_for_timeout(1500)
    if await dialogs.count() == 0:
        raise ValueError("积分明细未打开")
    return await dialogs.last.inner_text(timeout=8000)


async def _read_snapshot_from_page(page, rewards_url):
    await page.goto(rewards_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await stop_if_restricted(page)
    try:
        await page.wait_for_selector("text=积分明细", timeout=15000)
    except PlaywrightTimeout:
        try:
            await page.wait_for_selector("text=Points breakdown", timeout=5000)
        except PlaywrightTimeout:
            pass
    await page.wait_for_timeout(2000)

    body_text = await page.locator("body").inner_text()
    activity_match = re.search(
        r"(?im)^\s*(?:每日)?活动\s*[:：]?\s*(\d+)\s*/\s*3\b",
        body_text,
    )
    if not activity_match:
        activity_match = re.search(
            r"(?im)^\s*(?:daily\s+)?activities\s*[:：]?\s*(\d+)\s*/\s*3\b",
            body_text,
        )
    if not activity_match:
        raise ValueError("缺少每日活动进度或积分明细入口")

    details_text = await _open_points_details(page)
    total_match = re.search(r"今日积分\s*([\d,]+)", details_text)
    if not total_match:
        total_match = re.search(r"Today'?s points\s*([\d,]+)", details_text, re.IGNORECASE)
    search_match = re.search(
        r"(?:必应搜索|Bing searches?)\s*([\d,]+)\s*/\s*([\d,]+)",
        details_text,
        re.IGNORECASE,
    )
    if not total_match or not search_match:
        raise ValueError("缺少今日总分或必应搜索积分")

    snapshot = {
        "today_total": int(total_match.group(1).replace(",", "")),
        "search_earned": int(search_match.group(1).replace(",", "")),
        "search_max": int(search_match.group(2).replace(",", "")),
        "activities_done": int(activity_match.group(1)),
    }
    # 只有响应里明确写出这些字段才收录。没有它们就不能把分数换算成次数。
    combined_text = f"{details_text}\n{body_text}"
    for label in ("pointProgress", "pointProgressMax", "pointsPerSearch"):
        found = _labeled_int(combined_text, label)
        if found is not None:
            snapshot[label] = found
    return snapshot


async def get_browser_rewards_snapshot(context):
    """在独立页面读取 Rewards 进度，避免干扰搜索页并重试导航/点击故障。"""
    if not context_is_usable(context):
        log.warning("浏览器上下文已关闭，停止积分读取")
        return None
    urls = (
        "https://rewards.bing.com/earn",
        "https://rewards.bing.com/dashboard",
    )
    attempts = 4
    for attempt in range(attempts):
        page = None
        url = urls[attempt % len(urls)]
        try:
            page = await context.new_page()
            return await _read_snapshot_from_page(page, url)
        except AccountRestrictedError:
            raise
        except Exception as exc:
            if is_target_closed_error(exc) or not context_is_usable(context):
                log.warning("浏览器上下文已关闭，停止积分读取")
                return None
            log.warning(
                "Rewards 核验第 %d/%d 次读取失败 (%s): %s",
                attempt + 1, attempts, type(exc).__name__, exc,
            )
            if attempt < attempts - 1:
                await asyncio.sleep(2 * (attempt + 1))
        finally:
            if page is not None:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception as exc:
                    log.debug("Rewards 核验临时页面关闭失败: %s", exc)
    return None


def rewards_bearer_from_request(url, headers):
    if (urlsplit(url).hostname or "").casefold() != "prod.rewardsplatform.microsoft.com":
        return None
    authorization = next(
        (str(value).strip() for name, value in headers.items() if name.casefold() == "authorization"),
        "",
    )
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token.strip():
        return None
    return f"Bearer {token.strip()}"


def rewards_quotas_complete(snapshot):
    return snapshot["search_earned"] >= 60 and snapshot["activities_done"] >= 3


async def perform_reading_task(page, account_name, captured_token):
    """阅读任务：从浏览器截取 Bearer token → 直调 API 拿 30 分。"""
    log.info('='*50)
    log.info('尝试阅读任务 (API 直调) - %s', account_name)
    log.info('='*50)

    REWARDS_URL = 'https://rewards.bing.com/dashboard'

    try:
        try:
            await page.goto(REWARDS_URL, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT)
        except PlaywrightTimeout:
            await asyncio.sleep(3)
            await page.goto(REWARDS_URL, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT)
        await asyncio.sleep(5)
    except Exception as e:
        log.warning('加载 Rewards 失败: %s', e)

    token = captured_token["value"]

    if not token:
        log.warning('未捕获到 Bearer token，跳过阅读任务')
        return

    log.info('开始调用阅读积分 API...')

    headers = {
        'authorization': token,
        'x-rewards-appid': 'SAAndroid/32.6.2110003560',
        'x-rewards-ismobile': 'true',
        'x-rewards-country': 'cn',
        'x-rewards-language': 'en-US',
        'content-type': 'application/json; charset=utf-8',
        'user-agent': ('Mozilla/5.0 (Linux; Android 12; 2304FPN6DC Build/V417IR; ) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 '
                       'Mobile Safari/537.36 BingSapphire/32.6.2110003560'),
    }

    async with httpx.AsyncClient() as client:
        # 查进度
        try:
            resp = await client.get(
                'https://prod.rewardsplatform.microsoft.com/dapi/me?channel=SAAndroid&options=612',
                headers=headers, timeout=15,
            )
            data = resp.json()
            current = 0; max_articles = 10
            for p in data.get('response',{}).get('promotions',[]):
                if 'readarticle3_30points' in p.get('name',''):
                    a = p.get('attributes',{})
                    current = int(a.get('activityprogress','0'))
                    max_articles = int(a.get('activitymax','10'))
                    if a.get('complete') == 'True':
                        log.info('  阅读任务今日已完成')
                        return
                    break
            log.info('  进度: %d/%d', current, max_articles)
        except Exception as e:
            log.warning('查进度失败: %s，继续', e)
            current = 0; max_articles = 10

        for i in range(current, max_articles):
            body = {
                'amount': 1, 'country': 'cn',
                'id': hashlib.sha256(os.urandom(32)).hexdigest(),
                'type': 101,
                'attributes': {'offerid': 'ENUS_readarticle3_30points'},
            }
            try:
                resp = await client.post(
                    'https://prod.rewardsplatform.microsoft.com/dapi/me/activities',
                    headers=headers, json=body, timeout=15,
                )
                data = resp.json()
                if data.get('code') == 0:
                    if data.get('isDuplicate'):
                        log.info('  [%d/%d] 重复，跳过', i+1, max_articles)
                        continue
                    pts = data.get('response',{}).get('activity',{}).get('p',0)
                    bal = data.get('response',{}).get('balance','?')
                    log.info('  [%d/%d] ✅ +%d 分 (余额: %s)', i+1, max_articles, pts, bal)
                else:
                    log.warning('  [%d/%d] 失败: code=%d', i+1, max_articles, data.get('code'))
            except Exception as e:
                log.warning('  [%d/%d] 异常: %s', i+1, max_articles, e)

            await asyncio.sleep(3)

        log.info('阅读任务 API 调用完成')



async def run_one_account(account: dict, index: int, total: int, local: bool = False):
    """对单个账号执行完整的 Rewards 流程。

    返回值: "success" | "expired" | "error" | "restricted"
    """
    name = account.get("name", f"账号{index + 1}")
    session = account.get("session")
    raw_cookies = account.get("cookies", "")
    appkey = account.get("appkey", "")

    def save_progress(reason, complete, snapshot, searches_started):
        remember_checkpoint(
            name,
            now=datetime.now().astimezone(),
            directory=PROFILES_DIR,
            reason=reason,
            complete=complete,
            snapshot=snapshot,
            searches_started=searches_started,
        )

    log.info("=" * 50)
    log.info("账号 [%d/%d]: %s", index + 1, total, name)
    log.info("=" * 50)

    if not raw_cookies and not session:
        log.error("账号 %s 既没有 cookies 也没有 session", name)
        return "error"

    if not local:
        lock_info = inspect_profile_lock(os.path.join(PROFILES_DIR, name))
        if not profile_launch_allowed(lock_info):
            log.error(
                "持久化 profile 锁状态为 %s，拒绝启动，不删除锁，不并发打开",
                lock_info.get("state"),
            )
            return "error"

    use_session = bool(session)
    log.info("使用 %s 模式", "session（完整会话）" if use_session else "cookies（DevTools）")

    search_words = await fetch_hot_words(appkey)

    captured_token = {"value": None}
    token_logged = False

    def capture_rewards_token(request):
        nonlocal token_logged
        token = rewards_bearer_from_request(request.url, request.headers)
        if token:
            captured_token["value"] = token
            if not token_logged:
                log.info("捕获到 Rewards API Bearer token")
                token_logged = True

    async with async_playwright() as p:
        use_xvfb = os.getenv("USE_HEADED", "").lower() == "true"

        if local:
            # 本地测试：用系统 Chrome（headed），不要 Playwright 自带的 Chromium
            channel = None
            chrome_paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
            for cp in chrome_paths:
                if os.path.exists(cp):
                    channel = "chrome"
                    break
            launch_kw = {
                "headless": False,
                "channel": channel,
                "args": HEADLESS_ARGS,
            }
            log.info("浏览器模式: 本地 headed (%s)", channel or "chromium")
        else:
            launch_kw = {
                "headless": not use_xvfb,
                "args": XVFB_ARGS if use_xvfb else HEADLESS_ARGS,
            }
            chromium_path = os.getenv("CHROMIUM_PATH", "")
            if chromium_path:
                launch_kw["executable_path"] = chromium_path
            log.info("浏览器模式: %s", "headed (Xvfb)" if use_xvfb else "headless")
        browser_or_ctx = None
        try:
            if local:
                # 本地测试：用 session 快照 + system Chrome headed
                browser = await p.chromium.launch(**launch_kw)
                ctx_kwargs = {**DEFAULT_CONTEXT_OPTIONS, "viewport": {"width": 1920, "height": 1080}}
                if use_session:
                    ctx_kwargs["storage_state"] = session
                context = await browser.new_context(**ctx_kwargs)
                context.on("request", capture_rewards_token)
                await context.add_init_script(ANTI_DETECTION_SCRIPT)
                page = await context.new_page()
                if not use_session:
                    cookies = parse_cookies(raw_cookies)
                    await context.add_cookies(cookies)
                    log.info("已注入 %d 条 cookie", len(cookies))
                else:
                    log.info("已恢复完整浏览器会话")
                browser_or_ctx = browser
            else:
                # 服务器：用持久化 profile（Chrome 自动维护 token 续期）
                profile_dir = os.path.join(PROFILES_DIR, name)
                os.makedirs(PROFILES_DIR, exist_ok=True)
                has_cookies = (
                    os.path.exists(os.path.join(profile_dir, "Default", "Network", "Cookies")) or
                    os.path.exists(os.path.join(profile_dir, "Default", "Cookies"))
                )
                persistent_kw = {
                    "user_data_dir": profile_dir,
                    "headless": not use_xvfb,
                    "args": XVFB_ARGS if use_xvfb else HEADLESS_ARGS,
                    "viewport": {"width": 1920, "height": 1080},
                    "user_agent": USER_AGENT,
                    "locale": "zh-CN",
                    "timezone_id": "Asia/Shanghai",
                }
                chromium_path = os.getenv("CHROMIUM_PATH", "")
                if chromium_path:
                    persistent_kw["executable_path"] = chromium_path
                lock_info = inspect_profile_lock(profile_dir)
                if not profile_launch_allowed(lock_info):
                    log.error(
                        "持久化 profile 锁状态为 %s，拒绝启动，不删除锁，不并发打开",
                        lock_info.get("state"),
                    )
                    return "error"
                context = await p.chromium.launch_persistent_context(**persistent_kw)
                context.on("request", capture_rewards_token)
                await context.add_init_script(ANTI_DETECTION_SCRIPT)
                page = context.pages[0] if context.pages else await context.new_page()

                if has_cookies:
                    log.info("使用已有持久化 profile")
                else:
                    log.info("新建 profile（尚无 cookie 文件）")
                # 始终从 accounts.json 注入 cookie，覆盖 profile 中的旧值
                if session:
                    log.info("从 session 注入 cookie ...")
                    await page.goto(BING_URL, wait_until="domcontentloaded", timeout=20000)
                    await context.add_cookies(session["cookies"])
                    await page.goto(BING_URL, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(3)
                browser_or_ctx = context

            try:
                await page.goto(BING_URL, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
                await stop_if_restricted(page)
            except PlaywrightError as exc:
                if is_target_closed_error(exc):
                    log.error("浏览器上下文已关闭，停止且不记为会话过期或任务完成")
                    save_progress("context_closed", False, None, 0)
                    return "error"
                raise
            except AccountRestrictedError:
                log.error("检测到验证码或账号限制，立即停止（不绕过、不更换身份或代理），不判定完成")
                save_progress("restricted", False, None, 0)
                return "restricted"

            logged_in = await check_logged_in(page)
            if page_is_closed(page) or not context_is_usable(context):
                log.error("浏览器上下文已关闭，停止且不记为会话过期或任务完成")
                save_progress("context_closed", False, None, 0)
                return "error"
            if not logged_in:
                log.warning("❌ 账号 %s 会话已过期，跳过任务", name)
                return "expired"

            log.info("✅ 登录态有效")

            initial_snapshot = await get_browser_rewards_snapshot(context)
            now = datetime.now().astimezone()
            checkpoint_record = load_checkpoint_record(name)
            plan = plan_remaining_work(initial_snapshot, checkpoint_record, now)
            if plan["action"] == "skip":
                save_progress("verified_complete", True, initial_snapshot, 0)
                log.info("账号 %s 今日搜索和活动额度已完成，跳过重复任务", name)
                return "success"
            if plan["action"] == "stop":
                log.error(
                    "无法确认当前已入账进度（检查点=%s），停止且不按历史次数重放",
                    plan["checkpoint_relation"],
                )
                save_progress(plan["reason"], False, initial_snapshot, 0)
                return "error"

            # 时区 + 今日积分检查，积分满则等跨天后再执行
            local_now = datetime.now()
            bj_now = datetime.now(timezone(timedelta(hours=8)))
            log.info("本地时间: %s (UTC%s)  北京时间: %s (星期%s)",
                    local_now.strftime("%Y-%m-%d %H:%M"),
                    local_now.astimezone().strftime("%z"),
                    bj_now.strftime("%Y-%m-%d %H:%M"),
                    ["一","二","三","四","五","六","日"][bj_now.weekday()])

            MAX_WAIT_MINUTES = 30
            CHECK_INTERVAL = 300
            waited = 0

            while True:
                pts = await get_rewards_points(page)
                counters_str = ", ".join(f"{c['earned']}/{c['max']}" for c in pts.get("counters", []))
                log.info("今日积分: %s", counters_str or "未检测到")

                if not pts.get("all_maxed"):
                    log.info("积分未满，开始执行任务")
                    break

                if waited >= MAX_WAIT_MINUTES * 60:
                    log.warning("等待 %d 分钟后积分仍满，跳过此账号", MAX_WAIT_MINUTES)
                    return "expired"

                waited += CHECK_INTERVAL
                log.info("积分已满，可能尚未跨天，%d 分钟后重试...", CHECK_INTERVAL // 60)
                await asyncio.sleep(CHECK_INTERVAL)

            score_before = await get_browser_rewards_snapshot(context)
            if score_before:
                log.info(
                    "开跑前积分: 今日=%d, 必应搜索=%d/%d, 每日活动=%d/3",
                    score_before["today_total"], score_before["search_earned"],
                    score_before["search_max"], score_before["activities_done"],
                )
            else:
                log.error("开跑前积分基线读取失败，本账号最终不能判定为成功")

            now = datetime.now().astimezone()
            plan = plan_remaining_work(score_before, checkpoint_record, now)
            if plan["action"] == "skip":
                save_progress("verified_complete", True, score_before, 0)
                log.info("账号 %s 今日搜索和活动额度已完成，跳过重复任务", name)
                return "success"
            if plan["action"] != "run":
                log.error("开跑前无法确认当前已入账进度，停止且不重放搜索")
                save_progress(plan["reason"], False, score_before, 0)
                return "error"

            # 活动次数来自「n/3」计数。搜索必须由实读进度证明，不能把分数缺口当次数。
            try:
                if plan["activities"] == 0:
                    log.info("当前活动额度已满，跳过每日活动点击")
                else:
                    await click_daily_activities(page, max_clicks=plan["activities"])
            except BrowserClosedError:
                log.error("浏览器或上下文已关闭，停止本账号，不重开 profile，不判定完成")
                save_progress("context_closed", False, score_before, 0)
                return "error"
            except AccountRestrictedError:
                log.error("检测到验证码或账号限制，立即停止（不绕过、不更换身份或代理），不判定完成")
                save_progress("restricted", False, score_before, 0)
                return "restricted"
            except Exception as e:
                log.warning("每日活动执行失败（不影响搜索）: %s", e)

            score_after_activities = await get_browser_rewards_snapshot(context)
            if score_before and score_after_activities:
                activity_delta = score_after_activities["today_total"] - score_before["today_total"]
                log.info(
                    "每日活动阶段状态: %d/3, 当前总分增量 %+d；桌面搜索结束后再做最终核验",
                    score_after_activities["activities_done"], activity_delta,
                )
            elif score_after_activities:
                log.info(
                    "每日活动阶段状态: %d/3；缺少开跑前积分基线",
                    score_after_activities["activities_done"],
                )
            else:
                log.warning("每日活动阶段积分暂不可读，桌面搜索结束后再核验")

            async def refresh_search_snapshot():
                await stop_if_restricted(page)
                return await get_browser_rewards_snapshot(context)

            async def search_once():
                nonlocal page
                page = await perform_searches(page, search_words, context, search_limit=1)

            search_result = {"action": "stop", "completed": 0, "reason": "not_started", "complete": False}
            try:
                search_result = await run_refresh_bounded_searches(
                    refresh_search_snapshot, search_once,
                )
            except BrowserClosedError as exc:
                log.error(
                    "浏览器或上下文已关闭，停止本账号，不重开 profile，不判定完成（已结束的搜索尝试=%d）",
                    exc.completed,
                )
                save_progress("context_closed", False, score_before, exc.completed)
                return "error"
            except AccountRestrictedError:
                log.error("检测到验证码或账号限制，立即停止（不绕过、不更换身份或代理），不判定完成")
                save_progress("restricted", False, score_before, search_result["completed"])
                return "restricted"
            if search_result["action"] != "done":
                log.error(
                    "搜索进度无法安全确认（%s），停止，不把分数缺口换成次数，不判定完成",
                    search_result["reason"],
                )
                save_progress(search_result["reason"], False, score_before, search_result["completed"])
                return "error"

            rewards_verified = False
            activities_complete = False
            search_complete = False
            expected_points = None
            score_after_tasks = None
            if score_before:
                expected_points = (
                    max(0, 60 - score_before["search_earned"])
                    + max(0, 3 - score_before["activities_done"]) * 10
                )
                for attempt in range(3):
                    score_after_tasks = await get_browser_rewards_snapshot(context)
                    if score_after_tasks:
                        total_delta = score_after_tasks["today_total"] - score_before["today_total"]
                        activities_complete = score_after_tasks["activities_done"] >= 3
                        search_complete = score_after_tasks["search_earned"] >= 60
                        rewards_verified = rewards_quotas_complete(score_after_tasks)
                        if rewards_verified:
                            break
                    if attempt < 2:
                        log.info("任务完成后的积分尚未同步，10 秒后复核 (%d/3)", attempt + 1)
                        await asyncio.sleep(10)
            else:
                score_after_tasks = await get_browser_rewards_snapshot(context)
                if score_after_tasks:
                    activities_complete = score_after_tasks["activities_done"] >= 3
                    search_complete = score_after_tasks["search_earned"] >= 60
                    rewards_verified = rewards_quotas_complete(score_after_tasks)

            if score_before and score_after_tasks:
                total_delta = score_after_tasks["today_total"] - score_before["today_total"]
                log.info(
                    "最终积分核验: 必应搜索=%d/60, 活动=%d/3, 总分增量 %+d, 任务计数核验: %s",
                    score_after_tasks["search_earned"], score_after_tasks["activities_done"],
                    total_delta, "通过" if rewards_verified else "未通过",
                )
                if expected_points is not None and total_delta < expected_points and rewards_verified:
                    log.warning(
                        "搜索与活动计数已完成，但总积分增量低于预期 (+%d/+%d)；不重跑已完成任务",
                        total_delta, expected_points,
                    )
            elif score_after_tasks:
                log.info(
                    "最终积分核验（无开跑前基线）: 搜索=%d/60, 活动=%d/3, 任务计数核验: %s",
                    score_after_tasks["search_earned"], score_after_tasks["activities_done"],
                    "通过" if rewards_verified else "未通过",
                )
            else:
                log.error("最终积分核验失败：Rewards 页面数据不可读")

            save_progress(
                "verified_complete" if rewards_verified else "verified_incomplete",
                bool(rewards_verified),
                score_after_tasks,
                search_result["completed"],
            )

            # ── 阅读任务 (API 直调) ──
            try:
                await perform_reading_task(page, name, captured_token)
            except Exception as e:
                log.warning("阅读任务执行失败（不影响搜索）: %s", e)

            if not rewards_verified:
                if not score_after_tasks:
                    log.error(
                        "账号 %s Rewards 最终核验未通过（页面数据不可读，无法确认每日活动和桌面搜索）",
                        name,
                    )
                else:
                    log.error(
                        "账号 %s Rewards 最终核验未通过（每日活动=%s，桌面搜索=%s）",
                        name, "完成" if activities_complete else "未完成",
                        "完成" if search_complete else "未完成",
                    )
                return "error"
        finally:
            if browser_or_ctx:
                try:
                    await browser_or_ctx.close()
                except Exception:
                    log.debug("浏览器关闭时已不可用")


    log.info("账号 %s 完成", name)
    return "success"


async def main(local: bool = False, local_file: str = "", account_name: str = ""):
    if local:
        file_path = local_file or os.path.join(os.path.expanduser("~"), "Desktop", "bing_rewards_小号.txt")
        log.info("本地测试模式，读取账号文件: %s", file_path)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except Exception as e:
            log.error("读取文件失败: %s", e)
            return 1
        # 兼容 {}/[] 两种格式
        if raw.startswith("{"):
            raw = f"[{raw}]"
        accounts = json.loads(raw)
        log.info("加载了 %d 个账号（本地文件）", len(accounts))
    else:
        accounts = load_accounts()
        if not accounts:
            emit_run_notification(
                bool(account_name),
                False,
                "Bing Rewards 配置错误",
                "BING_ACCOUNTS 为空或解析失败",
                False,
            )
            return 1

    accounts, selection_problem = filter_accounts_for_invocation(accounts, account_name)
    if selection_problem:
        log.error("指定账号不可用，未执行任何账号")
        return 1
    if account_name:
        log.info("只执行指定账号，共 %d 个", len(accounts))

    total = len(accounts)
    results = {"success": [], "expired": [], "error": [], "restricted": []}
    stop_run = False

    for i, account in enumerate(accounts):
        name = account.get("name", f"账号{i + 1}")
        try:
            status = await run_one_account(account, i, total, local=local)
            if status == "restricted":
                results["restricted"].append(name)
                log.error("检测到验证码或账号限制，停止后续账号")
                stop_run = True
            else:
                results[status].append(name)
        except AccountRestrictedError:
            results["restricted"].append(name)
            log.error("检测到验证码或账号限制，停止后续账号")
            stop_run = True
        except BrowserClosedError:
            log.error("浏览器或上下文已关闭，停止本账号，不重开 profile，不判定完成")
            results["error"].append(name)
        except Exception as e:
            log.exception("账号 %s 异常: %s", name, e)
            results["error"].append(name)

        if stop_run:
            break
        if i < total - 1 and ACCOUNT_COOLDOWN > 0:
            log.info("等待 %d 秒冷却...", ACCOUNT_COOLDOWN)
            await asyncio.sleep(ACCOUNT_COOLDOWN)

    success_n = len(results["success"])
    expired_n = len(results["expired"])
    error_n = len(results["error"])
    restricted_n = len(results["restricted"])

    log.info("=" * 50)
    log.info(
        "完成: 成功 %d, 过期 %d, 失败 %d, 限制 %d, 总计 %d",
        success_n, expired_n, error_n, restricted_n, total,
    )

    # ── 推送通知（本地测试和选择性补跑都跳过） ──
    now_str = datetime.now().strftime("%m-%d %H:%M")
    if not local:
        lines = [f"Bing Rewards 执行完毕 ({now_str})", ""]
        if results["success"]:
            lines.append(f"✅ 成功 ({success_n}): {', '.join(results['success'])}")
        if results["expired"]:
            lines.append(f"⚠️ 会话过期 ({expired_n}): {', '.join(results['expired'])}")
            lines.append("")
            lines.append("更新方法: F12 → Network → 重新复制 Cookie 并更新 BING_ACCOUNTS；")
            lines.append("或运行 login_helper.py 重新生成 session。更新后手动重跑 Action。")
        if results["error"]:
            lines.append(f"❌ 执行失败 ({error_n}): {', '.join(results['error'])}")
        if results["restricted"]:
            lines.append(f"⛔ 账号限制 ({restricted_n}): {', '.join(results['restricted'])}")

        content = "\n".join(lines)
        failed = bool(expired_n or error_n or restricted_n)
        title = f"Bing Rewards {'✅' if not failed else '⚠️'} {success_n}/{total} ({now_str})"
        emit_run_notification(bool(account_name), local, title, content, not failed)
    return 0 if not (expired_n or error_n or restricted_n) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bing Rewards 每日任务自动化")
    parser.add_argument("--local", action="store_true", help="本地测试模式，读取桌面 bing_rewards_小号.txt")
    parser.add_argument("--file", default="", help="自定义账号文件路径")
    parser.add_argument("--account", default="", help="只执行指定账号名，例如 主号")
    args = parser.parse_args()

    log.info("Bing Rewards 多账号自动化启动 (local=%s, selective=%s)", args.local, bool(args.account))
    try:
        sys.exit(asyncio.run(main(local=args.local, local_file=args.file, account_name=args.account)))
    except Exception as e:
        log.exception("致命错误: %s", e)
        sys.exit(1)
