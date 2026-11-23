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


async def perform_searches(page, search_words, context):
    """执行搜索 — 顺便贡献 Edge 浏览时长（点击链接模拟真实浏览）。"""
    CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    log.info("开始 %d 次搜索", MAX_SEARCHES)
    for i in range(MAX_SEARCHES):
        word = search_words[i % len(search_words)]
        form_str = "".join(random.choices(CHARS, k=4))
        cvid_str = "".join(random.choices(CHARS, k=32))
        search_url = (
            f"https://cn.bing.com/search?q={url_encode(word)}"
            f"&form={form_str}&cvid={cvid_str}"
        )
        for recovery_attempt in range(2):
            try:
                # 先滚动到底部（模仿原脚本 smoothScrollToBottom 行为）
                await page.goto(BING_URL, wait_until="domcontentloaded", timeout=15000)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(random.uniform(0.5, 1.5))

                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                log.info("[%d/%d] %s", i + 1, MAX_SEARCHES, word[:30])

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
                    except Exception:
                        pass
            except PlaywrightTimeout:
                log.warning("[%d/%d] 搜索超时，跳过", i + 1, MAX_SEARCHES)
                break
            except PlaywrightError as exc:
                if "target page, context or browser has been closed" not in str(exc).casefold():
                    raise
                if recovery_attempt:
                    raise
                log.warning("[%d/%d] 浏览器页面已关闭，重开页面后重试当前搜索", i + 1, MAX_SEARCHES)
                page = await context.new_page()
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


async def click_daily_activities(page):
    """点击 Rewards 每日活动中的点击搜索项（每个 10 分），返回成功数。"""
    log.info("进入 Rewards 页面，查找每日活动...")
    try:
        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    except PlaywrightTimeout:
        log.warning("Rewards 页面加载超时")
        return 0
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
            for (const el of links.slice(0, 3)) {
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
    """)

    log.info("每日活动完成: 成功 %d/3", clicked)
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

    return {
        "today_total": int(total_match.group(1).replace(",", "")),
        "search_earned": int(search_match.group(1).replace(",", "")),
        "search_max": int(search_match.group(2).replace(",", "")),
        "activities_done": int(activity_match.group(1)),
    }


async def get_browser_rewards_snapshot(context):
    """在独立页面读取 Rewards 进度，避免干扰搜索页并重试导航/点击故障。"""
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
        except Exception as exc:
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

    返回值: "success" | "expired" | "error"
    """
    name = account.get("name", f"账号{index + 1}")
    session = account.get("session")
    raw_cookies = account.get("cookies", "")
    appkey = account.get("appkey", "")

    log.info("=" * 50)
    log.info("账号 [%d/%d]: %s", index + 1, total, name)
    log.info("=" * 50)

    if not raw_cookies and not session:
        log.error("账号 %s 既没有 cookies 也没有 session", name)
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

            await page.goto(BING_URL, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            if not await check_logged_in(page):
                log.warning("❌ 账号 %s 会话已过期，跳过任务", name)
                return "expired"

            log.info("✅ 登录态有效")

            initial_snapshot = await get_browser_rewards_snapshot(context)
            if initial_snapshot and rewards_quotas_complete(initial_snapshot):
                log.info("账号 %s 今日搜索和活动额度已完成，跳过重复任务", name)
                return "success"

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

            # 先点每日活动（3 个点击搜索项，各 10 分），再做 30 轮常规搜索
            # 活动失败不影响搜索，各自独立
            try:
                await click_daily_activities(page)
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

            page = await perform_searches(page, search_words, context)

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
                await browser_or_ctx.close()


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
            push_send("Bing Rewards 配置错误", "BING_ACCOUNTS 为空或解析失败", is_success=False)
            return 1

    if account_name:
        accounts = [a for a in accounts if a.get("name") == account_name]
        if not accounts:
            log.error("未找到账号: %s", account_name)
            return 1
        log.info("只执行指定账号: %s", account_name)

    total = len(accounts)
    results = {"success": [], "expired": [], "error": []}

    for i, account in enumerate(accounts):
        name = account.get("name", f"账号{i + 1}")
        try:
            status = await run_one_account(account, i, total, local=local)
            results[status].append(name)
        except Exception as e:
            log.exception("账号 %s 异常: %s", name, e)
            results["error"].append(name)

        if i < total - 1 and ACCOUNT_COOLDOWN > 0:
            log.info("等待 %d 秒冷却...", ACCOUNT_COOLDOWN)
            await asyncio.sleep(ACCOUNT_COOLDOWN)

    success_n = len(results["success"])
    expired_n = len(results["expired"])
    error_n = len(results["error"])

    log.info("=" * 50)
    log.info("完成: 成功 %d, 过期 %d, 失败 %d, 总计 %d", success_n, expired_n, error_n, total)

    # ── 推送通知（本地测试跳过） ──
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

        content = "\n".join(lines)
        title = f"Bing Rewards {'✅' if expired_n == 0 and error_n == 0 else '⚠️'} {success_n}/{total} ({now_str})"
        push_send(title, content, is_success=(expired_n == 0 and error_n == 0))
    return 0 if (expired_n == 0 and error_n == 0) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bing Rewards 每日任务自动化")
    parser.add_argument("--local", action="store_true", help="本地测试模式，读取桌面 bing_rewards_小号.txt")
    parser.add_argument("--file", default="", help="自定义账号文件路径")
    parser.add_argument("--account", default="", help="只执行指定账号名，例如 主号")
    args = parser.parse_args()

    log.info("Bing Rewards 多账号自动化启动 (local=%s, account=%s)", args.local, args.account or "全部")
    try:
        sys.exit(asyncio.run(main(local=args.local, local_file=args.file, account_name=args.account)))
    except Exception as e:
        log.exception("致命错误: %s", e)
        sys.exit(1)
