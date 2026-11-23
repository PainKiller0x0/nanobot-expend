from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
from playwright.async_api import Error as PlaywrightError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bing_rewards import (
    get_browser_rewards_snapshot,
    perform_searches,
    rewards_bearer_from_request,
    rewards_quotas_complete,
)


class FakeLocator:
    def __init__(self, text="", count=1):
        self.text = text
        self.count_value = count

    @property
    def last(self):
        return self

    async def count(self):
        return self.count_value

    async def click(self, **kwargs):
        return None

    async def wait_for(self, **kwargs):
        return None

    async def scroll_into_view_if_needed(self):
        return None

    async def evaluate(self, script):
        return None

    async def inner_text(self, **kwargs):
        return self.text


class FakePage:
    def __init__(self, fail_navigation=False):
        self.fail_navigation = fail_navigation
        self.closed = False

    async def goto(self, *args, **kwargs):
        if self.fail_navigation:
            raise RuntimeError("navigation interrupted")

    async def wait_for_timeout(self, milliseconds):
        return None

    async def wait_for_selector(self, *args, **kwargs):
        return None

    def locator(self, selector):
        if selector == "body":
            return FakeLocator("\u6bcf\u65e5\u6d3b\u52a8 3/3")
        return FakeLocator("\u4eca\u65e5\u79ef\u5206 150\n\u5fc5\u5e94\u641c\u7d22 60/60")

    def get_by_text(self, text, exact=False):
        return FakeLocator()

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        page = FakePage(fail_navigation=not self.pages)
        self.pages.append(page)
        return page


class FakeSearchLinks:
    async def count(self):
        return 0


class FakeSearchPage:
    def __init__(self, close_on_first_navigation=False):
        self.close_on_first_navigation = close_on_first_navigation
        self.goto_count = 0
        self.closed = False

    async def goto(self, *args, **kwargs):
        self.goto_count += 1
        if self.close_on_first_navigation and self.goto_count == 1:
            self.closed = True
            raise PlaywrightError("Target page, context or browser has been closed")

    async def evaluate(self, script):
        return None

    def locator(self, selector):
        return FakeSearchLinks()


class FakeSearchContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        page = FakeSearchPage()
        self.pages.append(page)
        return page


class RewardsHelperTests(unittest.TestCase):
    def test_complete_counters_pass_even_if_total_delta_is_short(self):
        snapshot = {
            "today_total": 132,
            "search_earned": 60,
            "search_max": 60,
            "activities_done": 3,
        }
        self.assertTrue(rewards_quotas_complete(snapshot))

    def test_incomplete_search_quota_fails(self):
        self.assertFalse(rewards_quotas_complete({
            "search_earned": 57,
            "search_max": 60,
            "activities_done": 3,
        }))

    def test_incomplete_activities_fail(self):
        self.assertFalse(rewards_quotas_complete({
            "search_earned": 60,
            "search_max": 60,
            "activities_done": 2,
        }))

    def test_bearer_parser_accepts_case_insensitive_header_and_scheme(self):
        self.assertEqual(
            rewards_bearer_from_request(
                "https://prod.rewardsplatform.microsoft.com/dapi/me",
                {"Authorization": "bearer test-token"},
            ),
            "Bearer test-token",
        )

    def test_bearer_parser_rejects_other_hosts_and_non_bearer_auth(self):
        self.assertIsNone(rewards_bearer_from_request(
            "https://example.com/prod.rewardsplatform.microsoft.com",
            {"authorization": "Bearer test-token"},
        ))
        self.assertIsNone(rewards_bearer_from_request(
            "https://prod.rewardsplatform.microsoft.com/dapi/me",
            {"authorization": "Basic test-token"},
        ))


class RewardsSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_race_retries_in_a_fresh_verification_page(self):
        context = FakeContext()

        with patch("bing_rewards.asyncio.sleep", new=AsyncMock()):
            snapshot = await get_browser_rewards_snapshot(context)

        self.assertEqual(snapshot, {
            "today_total": 150,
            "search_earned": 60,
            "search_max": 60,
            "activities_done": 3,
        })
        self.assertEqual(len(context.pages), 2)
        self.assertTrue(all(page.closed for page in context.pages))


class SearchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_page_is_reopened_and_current_search_retried(self):
        old_page = FakeSearchPage(close_on_first_navigation=True)
        context = FakeSearchContext()

        with (
            patch("bing_rewards.MAX_SEARCHES", 1),
            patch("bing_rewards.asyncio.sleep", new=AsyncMock()),
            patch("bing_rewards.random.uniform", return_value=0.1),
        ):
            page = await perform_searches(old_page, ["test query"], context)

        self.assertEqual(old_page.goto_count, 1)
        self.assertEqual(len(context.pages), 1)
        self.assertEqual(context.pages[0].goto_count, 2)
        self.assertIs(page, context.pages[0])


if __name__ == "__main__":
    unittest.main()
