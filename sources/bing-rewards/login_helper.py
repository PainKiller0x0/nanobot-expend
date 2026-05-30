#!/usr/bin/env python3
"""Bing Rewards 登录助手 — 保存完整浏览器会话到桌面。

用法:
  python login_helper.py                # 主号，用 Playwright 自带 Chromium
  python login_helper.py --name 小号    # 第二个号
  python login_helper.py --edge         # 用系统 Edge（不稳定，不推荐）

首次使用:
  pip install playwright
  脚本会自动下载 Chromium（约 150MB，仅首次）
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys

# ── 内联常量（方便单文件分发） ──
BING_URL = "https://www.bing.com"
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
HEADFUL_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--window-size=1280,800",
]

DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")


async def check_logged_in(page) -> bool:
    """检查当前 page 是否已登录 Bing Rewards（两次尝试）。"""
    try:
        await page.locator('[id*="id_r"], [aria-label*="Rewards"]').first.is_visible(timeout=5000)
        return True
    except Exception:
        pass
    try:
        await page.goto(BING_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)
        await page.locator(
            '[id*="id_r"], [id*="id_l"], [aria-label*="Rewards"], #microsoft_rewards'
        ).first.is_visible(timeout=5000)
        return True
    except Exception:
        return False


def ensure_playwright():
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        print("正在安装 playwright...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
        print()


def ensure_chromium():
    """确保 Playwright 自带的 Chromium 已安装。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise Exception("not installed")
    except Exception:
        print("正在下载 Chromium（约 150MB，仅首次需要）...")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        print("下载完成！\n")


def _auto_detect_channel():
    """自动检测系统浏览器，优先级：Chrome > 无（用 Playwright Chromium）。"""
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in chrome_paths:
        if os.path.exists(p):
            return "chrome"
    return None


def _find_edge():
    """查找 Edge 浏览器，找不到返回 False。"""
    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in edge_paths:
        if os.path.exists(p):
            return True
    return False


async def main(name: str, channel: str = None):
    ensure_playwright()
    from playwright.async_api import async_playwright

    if channel is None:
        channel = _auto_detect_channel()

    if channel == "chrome":
        label = "Google Chrome（系统自带）"
    elif channel == "msedge":
        if not _find_edge():
            print("未找到 Edge，改用默认 Chromium。")
            channel = None
        else:
            label = "Microsoft Edge"
    if channel is None:
        ensure_chromium()
        label = "Chromium (Playwright 自带)"
    print(f"\n{'=' * 50}")
    print(f"  Bing Rewards 登录助手 — {name}")
    print(f"  浏览器: {label}")
    print(f"{'=' * 50}\n")
    print("即将打开浏览器，请手动登录微软账号（支持扫码/密码/验证码）。\n")

    user_data_dir = os.path.join(os.path.expanduser("~"), ".bing_rewards_profile")
    if os.path.exists(user_data_dir):
        shutil.rmtree(user_data_dir, ignore_errors=True)

    async with async_playwright() as p:
        persistent_kwargs = {
            "user_data_dir": user_data_dir,
            "headless": False,
            "args": HEADFUL_ARGS,
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 800},
            "locale": "zh-CN",
        }
        if channel:
            persistent_kwargs["channel"] = channel

        context = await p.chromium.launch_persistent_context(**persistent_kwargs)
        await context.add_init_script(ANTI_DETECTION_SCRIPT)

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(BING_URL, wait_until="domcontentloaded")

        print("→ 浏览器已打开，请在浏览器中完成登录。")
        print("→ 登录成功后，回到这里按 Enter 继续...")
        input()

        # 尝试检测登录态（浏览器可能已崩，持久化目录可恢复）
        logged_in = False
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(BING_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)
            logged_in = await check_logged_in(page)
        except Exception:
            print("\n⚠️  浏览器已关闭，从持久化目录恢复会话...")
            try:
                await context.close()
            except Exception:
                pass
            context = await p.chromium.launch_persistent_context(**persistent_kwargs)

        if not logged_in:
            print("\n⚠️  未检测到 Rewards 登录态，但继续保存当前会话。")

        try:
            state = await context.storage_state()
            # 只保留 cookies，去掉 origins（localStorage 太大导致超出 GitHub Secret 48KB 限制）
            state.pop("origins", None)
        except Exception as e:
            print(f"\n❌ 保存会话失败: {e}")
            print("请重新运行脚本再试。")
            await context.close()
            shutil.rmtree(user_data_dir, ignore_errors=True)
            return

        # 输出单个账号对象，多账号时手动用 [] 和 , 合并
        account_entry = {"name": name, "session": state, "appkey": ""}
        output = json.dumps(account_entry, indent=2, ensure_ascii=False)

        filename = f"bing_rewards_{name}.txt"
        filepath = os.path.join(DESKTOP, filename)
        if os.path.exists(filepath):
            os.remove(filepath)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(output)

        await context.close()
        shutil.rmtree(user_data_dir, ignore_errors=True)

        print(f"\n✅ 会话已保存到桌面: {filename}")
        print(f"\n── 单账号 ──")
        print(f"   用 [ ] 包住 {filename} 的内容，贴到 BING_ACCOUNTS")
        print(f"   即: [<文件内容>]")
        print(f"\n── 多账号 ──")
        print(f"   每个号运行一次本脚本，然后把多个文件内容用 , 拼接，再用 [ ] 包住：")
        print(f"   [<主号文件内容>, <小号文件内容>]\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bing Rewards 登录助手")
    parser.add_argument("--name", default="主号", help="账号名称")
    parser.add_argument("--edge", action="store_true", help="使用系统 Edge（不稳定，不推荐）")
    parser.add_argument("--chrome", action="store_true", help="使用系统 Chrome")
    args = parser.parse_args()

    channel = None
    if args.edge:
        channel = "msedge"
    elif args.chrome:
        channel = "chrome"

    asyncio.run(main(args.name, channel))
