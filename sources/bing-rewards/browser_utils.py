"""Bing Rewards 公共浏览器工具 — bing_rewards.py 和 login_helper.py 共用。"""

# ── 常量 ──────────────────────────────────────────────
BING_URL = "https://www.bing.com"
REWARDS_URL = "https://rewards.bing.com/dashboard"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

ANTI_DETECTION_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
"""

# Chromium 启动参数
HEADLESS_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1920,1080",
]

HEADFUL_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--window-size=1280,800",
]

# Xvfb 虚拟显示器 + headed 模式（比 headless 更不容易被检测）
XVFB_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--window-size=1920,1080",
]

DEFAULT_CONTEXT_OPTIONS = {
    "user_agent": USER_AGENT,
    "locale": "zh-CN",
    "timezone_id": "Asia/Shanghai",
}


async def create_browser(playwright, headless: bool = True):
    """启动 Chromium，返回 browser 实例。"""
    return await playwright.chromium.launch(
        headless=headless,
        args=HEADLESS_ARGS if headless else HEADFUL_ARGS,
    )


async def create_context(browser, storage_state=None, viewport=None):
    """创建浏览器上下文，注入反检测脚本，返回 context + page。"""
    kwargs = {**DEFAULT_CONTEXT_OPTIONS}
    if viewport:
        kwargs["viewport"] = viewport
    if storage_state:
        kwargs["storage_state"] = storage_state

    context = await browser.new_context(**kwargs)
    await context.add_init_script(ANTI_DETECTION_SCRIPT)
    page = await context.new_page()
    return context, page


async def check_logged_in(page) -> bool:
    """检查当前 page 是否已登录 Bing Rewards。

    返回 True 表示检测到 Rewards 徽章，登录态有效。
    """
    try:
        await page.locator('[id*="id_r"], [aria-label*="Rewards"]').first.is_visible(timeout=5000)
        return True
    except Exception:
        pass
    # 再试一次
    try:
        await page.goto(BING_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)
        await page.locator(
            '[id*="id_r"], [id*="id_l"], [aria-label*="Rewards"], #microsoft_rewards'
        ).first.is_visible(timeout=5000)
        return True
    except Exception:
        return False


async def get_rewards_points(page) -> dict:
    """从 Rewards 面板提取今日积分概况。

    返回 {"counters": [{"earned": 90, "max": 150}, ...], "all_maxed": bool}
    all_maxed 为 True 表示所有检测到的积分进度均已满（可能尚未跨天/已执行过）。
    """
    try:
        # networkidle 等 SPA 渲染完，页面是 React 写的
        await page.goto(REWARDS_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(5000)

        import re
        counters = []
        seen = set()

        # 1) 优先从页面所有可见文本中匹配 "X / Y"
        text = await page.locator("body").inner_text()
        for line in text.splitlines():
            for m in re.finditer(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)", line.strip()):
                pair = m.group(0)
                if pair in seen:
                    continue
                seen.add(pair)
                earned = int(m.group(1).replace(",", ""))
                maximum = int(m.group(2).replace(",", ""))
                if maximum > 0:
                    counters.append({"earned": earned, "max": maximum})

        # 2) 如果没找到，尝试取页面 title（经常含积分概览）
        if not counters:
            title = await page.title()
            for m in re.finditer(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)", title):
                earned = int(m.group(1).replace(",", ""))
                maximum = int(m.group(2).replace(",", ""))
                if maximum > 0:
                    counters.append({"earned": earned, "max": maximum})

        if not counters:
            return {"counters": [], "all_maxed": False}

        all_maxed = all(c["earned"] >= c["max"] for c in counters)
        return {"counters": counters, "all_maxed": all_maxed}
    except Exception:
        return {"counters": [], "all_maxed": False, "error": True}
