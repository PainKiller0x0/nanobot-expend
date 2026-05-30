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
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as url_encode

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from browser_utils import (
    BING_URL, REWARDS_URL, HEADLESS_ARGS, XVFB_ARGS, DEFAULT_CONTEXT_OPTIONS,
    ANTI_DETECTION_SCRIPT, check_logged_in, get_rewards_points,
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


async def perform_searches(page, search_words):
    """执行搜索 — 参照原 GreasyFork 脚本: 直接用 URL + 随机 form/cvid 参数。"""
    CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    log.info("开始 %d 次搜索", MAX_SEARCHES)
    for i in range(MAX_SEARCHES):
        word = search_words[i % len(search_words)]
        form_str = "".join(random.choices(CHARS, k=4))
        cvid_str = "".join(random.choices(CHARS, k=32))
        search_url = (
            f"https://www.bing.com/search?q={url_encode(word)}"
            f"&form={form_str}&cvid={cvid_str}"
        )
        try:
            # 先滚动到底部（模仿原脚本 smoothScrollToBottom 行为）
            await page.goto(BING_URL, wait_until="domcontentloaded", timeout=15000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(random.uniform(0.5, 1.5))

            await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            log.info("[%d/%d] %s", i + 1, MAX_SEARCHES, word[:30])
        except PlaywrightTimeout:
            log.warning("[%d/%d] 搜索超时，跳过", i + 1, MAX_SEARCHES)
            continue

        delay = random.uniform(MIN_SEARCH_DELAY, MAX_SEARCH_DELAY)
        # 每 PAUSE_EVERY_N 次搜索插入额外长暂停（原脚本 PAUSE_TIME 机制）
        if (i + 1) % PAUSE_EVERY_N == 0:
            delay += PAUSE_DURATION
        await asyncio.sleep(delay)
    log.info("搜索任务完成")


async def handle_quiz_page(page):
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
        browser = await p.chromium.launch(**launch_kw)
        try:
            ctx_kwargs = {**DEFAULT_CONTEXT_OPTIONS, "viewport": {"width": 1920, "height": 1080}}
            if use_session:
                ctx_kwargs["storage_state"] = session

            context = await browser.new_context(**ctx_kwargs)
            await context.add_init_script(ANTI_DETECTION_SCRIPT)
            page = await context.new_page()

            if not use_session:
                cookies = parse_cookies(raw_cookies)
                await context.add_cookies(cookies)
                log.info("已注入 %d 条 cookie", len(cookies))
            else:
                log.info("已恢复完整浏览器会话")

            await page.goto(BING_URL, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            if not await check_logged_in(page):
                log.warning("❌ 账号 %s 会话已过期，跳过任务", name)
                return "expired"

            log.info("✅ 登录态有效")

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

            # 先点每日活动（3 个点击搜索项，各 10 分），再做 30 轮常规搜索
            await click_daily_activities(page)
            await perform_searches(page, search_words)
        finally:
            await browser.close()

    log.info("账号 %s 完成", name)
    return "success"


async def main(local: bool = False, local_file: str = ""):
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
    args = parser.parse_args()

    log.info("Bing Rewards 多账号自动化启动 (local=%s)", args.local)
    try:
        sys.exit(asyncio.run(main(local=args.local, local_file=args.file)))
    except Exception as e:
        log.exception("致命错误: %s", e)
        sys.exit(1)
