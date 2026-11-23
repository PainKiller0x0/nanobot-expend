from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from playwright.async_api import Error as PlaywrightError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bing_rewards import (
    MAX_SEARCHES,
    AccountRestrictedError,
    BrowserClosedError,
    CHECKPOINT_FILENAME,
    click_daily_activities,
    emit_run_notification,
    filter_accounts_for_invocation,
    get_browser_rewards_snapshot,
    inspect_profile_lock,
    load_checkpoint_record,
    perform_reading_task,
    perform_searches,
    plan_remaining_work,
    profile_launch_allowed,
    proven_search_remaining,
    record_checkpoint,
    run_refresh_bounded_searches,
    rewards_bearer_from_request,
    rewards_day_keys,
    rewards_quotas_complete,
    run_one_account,
    checkpoint_period_relation,
    search_step_status,
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


class ClosedOnGotoPage(FakeSearchPage):
    def __init__(self):
        super().__init__()
        self.url = "https://cn.bing.com/"

    async def goto(self, *args, **kwargs):
        self.goto_count += 1
        self.closed = True
        raise PlaywrightError("Target page, context or browser has been closed")

    async def title(self):
        return "Bing"


class ReopeningContext:
    def __init__(self, fail_new_page=False, closed=False):
        self.fail_new_page = fail_new_page
        self.closed = closed
        self.calls = 0
        self.pages = []

    def is_closed(self):
        return self.closed

    async def new_page(self):
        self.calls += 1
        if self.fail_new_page:
            raise PlaywrightError("Target page, context or browser has been closed")
        page = ClosedOnGotoPage()
        self.pages.append(page)
        return page


class QuietPage:
    def __init__(self, url="https://cn.bing.com/", title="Bing"):
        self.url = url
        self.title_text = title
        self.goto_count = 0
        self.script = ""

    async def goto(self, *args, **kwargs):
        self.goto_count += 1

    async def title(self):
        return self.title_text

    async def evaluate(self, script):
        self.script = script
        return 0

    def locator(self, selector):
        return FakeSearchLinks()


class ContextClosureTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_context_does_not_reopen_or_continue(self):
        page = ClosedOnGotoPage()
        context = ReopeningContext(closed=True)

        with (
            patch("bing_rewards.MAX_SEARCHES", 5),
            patch("bing_rewards.asyncio.sleep", new=AsyncMock()),
            patch("bing_rewards.random.uniform", return_value=0.1),
        ):
            with self.assertRaises(BrowserClosedError) as caught:
                await perform_searches(page, ["query"], context, search_limit=5)

        self.assertEqual(caught.exception.completed, 0)
        self.assertEqual(context.calls, 0)
        self.assertEqual(page.goto_count, 1)

    async def test_new_page_failure_is_bounded_to_one_attempt(self):
        page = ClosedOnGotoPage()
        context = ReopeningContext(fail_new_page=True)

        with (
            patch("bing_rewards.MAX_SEARCHES", 5),
            patch("bing_rewards.asyncio.sleep", new=AsyncMock()),
            patch("bing_rewards.random.uniform", return_value=0.1),
        ):
            with self.assertRaises(BrowserClosedError):
                await perform_searches(page, ["query"], context)

        self.assertEqual(context.calls, 1)
        self.assertEqual(page.goto_count, 1)

    async def test_reopened_page_closure_does_not_start_another_search(self):
        page = ClosedOnGotoPage()
        context = ReopeningContext()

        with (
            patch("bing_rewards.MAX_SEARCHES", 5),
            patch("bing_rewards.asyncio.sleep", new=AsyncMock()),
            patch("bing_rewards.random.uniform", return_value=0.1),
        ):
            with self.assertRaises(BrowserClosedError):
                await perform_searches(page, ["query"], context, search_limit=5)

        self.assertEqual(context.calls, 1)
        self.assertEqual(page.goto_count, 1)
        self.assertEqual(context.pages[0].goto_count, 1)

    async def test_search_limit_cannot_exceed_configured_maximum(self):
        page = QuietPage()
        context = FakeSearchContext()

        with (
            patch("bing_rewards.MAX_SEARCHES", 2),
            patch("bing_rewards.asyncio.sleep", new=AsyncMock()),
            patch("bing_rewards.random.uniform", return_value=0.1),
        ):
            await perform_searches(page, ["query"], context, search_limit=100)

        self.assertEqual(page.goto_count, 4)

    async def test_captcha_or_restriction_stops_immediately(self):
        page = QuietPage(url="https://www.bing.com/captcha/challenge", title="Verify")
        context = ReopeningContext()

        with (
            patch("bing_rewards.MAX_SEARCHES", 3),
            patch("bing_rewards.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaises(AccountRestrictedError):
                await perform_searches(page, ["query"], context, search_limit=3)

        self.assertEqual(page.goto_count, 1)
        self.assertEqual(context.calls, 0)
        self.assertEqual(page.script, "")

    async def test_suspended_title_stops_without_url_captcha(self):
        page = QuietPage(url="https://cn.bing.com/", title="Account suspended")

        with patch("bing_rewards.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(AccountRestrictedError):
                await perform_searches(page, ["query"], FakeSearchContext(), search_limit=2)

        self.assertEqual(page.goto_count, 1)

    async def test_closed_context_snapshot_is_not_retried(self):
        context = ReopeningContext(closed=True)
        with patch("bing_rewards.asyncio.sleep", new=AsyncMock()):
            self.assertIsNone(await get_browser_rewards_snapshot(context))
        self.assertEqual(context.calls, 0)

    async def test_closed_new_page_snapshot_stops_after_one_attempt(self):
        context = ReopeningContext(fail_new_page=True)
        with patch("bing_rewards.asyncio.sleep", new=AsyncMock()):
            self.assertIsNone(await get_browser_rewards_snapshot(context))
        self.assertEqual(context.calls, 1)


class ActivityLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_clicks_follow_remaining_count_and_stop_on_captcha(self):
        page = QuietPage(url="https://rewards.bing.com/dashboard", title="Rewards")
        with patch("bing_rewards.asyncio.sleep", new=AsyncMock()):
            clicked = await click_daily_activities(page, max_clicks=2)
        self.assertEqual(clicked, 0)
        self.assertIn("links.slice(0, 2)", page.script)
        self.assertNotIn("links.slice(0, 3)", page.script)

        blocked = QuietPage(url="https://account.live.com/abuse", title="restricted")
        with patch("bing_rewards.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(AccountRestrictedError):
                await click_daily_activities(blocked, max_clicks=3)
        self.assertEqual(blocked.script, "")


NOW = datetime(2026, 11, 23, 3, 34, tzinfo=timezone(timedelta(hours=8)))
DONE = {"search_earned": 60, "search_max": 60, "activities_done": 3, "today_total": 90}
PARTIAL = {"search_earned": 40, "search_max": 60, "activities_done": 1, "today_total": 40}


def _proven(progress, progress_max=60, per_action=5, activities=3, today=None):
    return {
        "search_earned": progress,
        "search_max": progress_max,
        "activities_done": activities,
        "today_total": progress if today is None else today,
        "pointProgress": progress,
        "pointProgressMax": progress_max,
        "pointsPerSearch": per_action,
    }


class CreditAndCheckpointTests(unittest.TestCase):
    def test_point_gap_is_not_a_search_count(self):
        self.assertEqual(MAX_SEARCHES, 30)
        self.assertIsNone(proven_search_remaining({
            "search_earned": 0, "search_max": 500, "activities_done": 0,
        }))
        self.assertIsNone(proven_search_remaining({
            "search_earned": 59, "search_max": 90, "activities_done": 0,
        }))
        self.assertIsNone(proven_search_remaining(PARTIAL))
        self.assertEqual(proven_search_remaining(DONE), 0)
        self.assertIsNone(proven_search_remaining({
            "search_earned": 40,
            "search_max": 60,
            "activities_done": 3,
            "pointProgress": 40,
            "pointProgressMax": 60,
            "pointsPerSearch": 7,
        }))
        self.assertIsNone(proven_search_remaining({
            "search_earned": 41,
            "search_max": 60,
            "activities_done": 3,
            "pointProgress": 40,
            "pointProgressMax": 60,
            "pointsPerSearch": 5,
        }))
        self.assertIsNone(proven_search_remaining({
            "search_earned": 40,
            "search_max": 60,
            "activities_done": 3,
            "pointProgress": True,
            "pointProgressMax": 60,
            "pointsPerSearch": 5.0,
        }))
        self.assertEqual(proven_search_remaining(_proven(40)), 4)
        self.assertLessEqual(proven_search_remaining(_proven(40)), MAX_SEARCHES)

    def test_finished_accounts_are_not_replayed_and_attempt_log_is_not_credit(self):
        accounts = [{"name": "one"}, {"name": "two"}, {"name": "three"}]
        selected, problem = filter_accounts_for_invocation(accounts, "three")
        self.assertIsNone(problem)
        self.assertEqual([item["name"] for item in selected], ["three"])
        same_day = {"complete": True, "searches_started": 26, "day_keys": rewards_day_keys(NOW)}
        self.assertEqual(plan_remaining_work(DONE, same_day, NOW)["action"], "skip")
        partial_plan = plan_remaining_work(PARTIAL, same_day, NOW)
        self.assertEqual(partial_plan["action"], "stop")
        self.assertIsNone(partial_plan["searches"])
        self.assertFalse(partial_plan["complete"])
        proven_plan = plan_remaining_work(_proven(40, activities=1), same_day, NOW)
        self.assertEqual(proven_plan["action"], "run")
        self.assertEqual(proven_plan["searches"], 4)
        self.assertEqual(proven_plan["activities"], 2)
        self.assertNotEqual(proven_plan["searches"], 20)

    def test_missing_or_duplicate_selector_does_not_run_accounts(self):
        accounts = [{"name": "three"}, {"name": "three"}]
        selected, problem = filter_accounts_for_invocation(accounts, "missing")
        self.assertEqual(problem, "missing")
        self.assertEqual(selected, [])
        selected, problem = filter_accounts_for_invocation(accounts, "three")
        self.assertEqual(problem, "duplicate")
        self.assertEqual(selected, [])
        untouched, problem = filter_accounts_for_invocation(accounts, "")
        self.assertIsNone(problem)
        self.assertEqual(len(untouched), 2)

    def test_reset_and_unknown_checkpoints_are_not_completion(self):
        reset_record = {
            "complete": True,
            "searches_started": 26,
            "day_keys": {
                "local_date": "2026-11-22",
                "shanghai_date": "2026-11-22",
                "rewards_timezone": "unverified",
            },
        }
        self.assertEqual(checkpoint_period_relation(reset_record, NOW), "reset")
        reset_plan = plan_remaining_work(None, reset_record, NOW)
        self.assertEqual(reset_plan["action"], "stop")
        self.assertFalse(reset_plan["complete"])
        self.assertIsNone(reset_plan["searches"])
        live_after_reset = plan_remaining_work(
            {"search_earned": 50, "search_max": 60, "activities_done": 3, "today_total": 80},
            reset_record,
            NOW,
        )
        self.assertEqual(live_after_reset["action"], "stop")
        self.assertIsNone(live_after_reset["searches"])
        self.assertFalse(live_after_reset["complete"])
        self.assertEqual(live_after_reset["checkpoint_relation"], "reset")

        self.assertEqual(checkpoint_period_relation({"complete": True}, NOW), "unknown")
        unknown = plan_remaining_work(None, {"complete": True, "searches_started": 26}, NOW)
        self.assertEqual(unknown["action"], "stop")
        self.assertFalse(unknown["complete"])

        same_day = {"complete": True, "searches_started": 30, "day_keys": rewards_day_keys(NOW)}
        self.assertEqual(
            checkpoint_period_relation(same_day, NOW),
            "same_calendar_dates_timezone_unverified",
        )
        self.assertFalse(plan_remaining_work(None, same_day, NOW)["complete"])

    def test_day_keys_keep_the_rewards_timezone_unverified(self):
        keys = rewards_day_keys(datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(keys["rewards_timezone"], "unverified")
        self.assertEqual(keys["local_date"], "2026-11-22")
        self.assertEqual(keys["shanghai_date"], "2026-11-23")
        with self.assertRaises(ValueError):
            rewards_day_keys(datetime(2026, 11, 23, 3, 34))

    def test_checkpoint_file_has_no_account_name_and_cannot_force_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_checkpoint(
                "account-3-secret",
                now=NOW,
                directory=tmp,
                reason="context_closed",
                complete=True,
                snapshot=PARTIAL,
                searches_started=26,
            )
            raw = (Path(tmp) / CHECKPOINT_FILENAME).read_text(encoding="utf-8")
            self.assertNotIn("account-3-secret", raw)
            self.assertNotIn("cookie", raw.casefold())
            self.assertNotIn("token", raw.casefold())
            loaded = load_checkpoint_record("account-3-secret", tmp)
            self.assertFalse(loaded["complete"])
            self.assertEqual(loaded["searches_started"], 26)
            self.assertFalse(plan_remaining_work(None, loaded, NOW)["complete"])

            record_checkpoint(
                "account-3-secret",
                now=NOW,
                directory=tmp,
                reason="verified_complete",
                complete=True,
                snapshot=DONE,
                searches_started=0,
            )
            verified = load_checkpoint_record("account-3-secret", tmp)
            self.assertTrue(verified["complete"])
            self.assertFalse(plan_remaining_work(None, verified, NOW)["complete"])
            self.assertEqual(plan_remaining_work(DONE, verified, NOW)["action"], "skip")


class ProfileLockTests(unittest.TestCase):
    def test_live_stale_and_unknown_locks_are_not_removed_or_launched(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "SingletonLock"
            lock.symlink_to("host-4321")
            with (
                patch("bing_rewards.os.remove", side_effect=AssertionError("remove")),
                patch("bing_rewards.os.unlink", side_effect=AssertionError("unlink")),
            ):
                live = inspect_profile_lock(tmp, pid_alive=lambda pid: True)
                stale = inspect_profile_lock(tmp, pid_alive=lambda pid: False)
            self.assertEqual(live["state"], "live")
            self.assertEqual(stale["state"], "stale")
            self.assertFalse(profile_launch_allowed(live))
            self.assertFalse(profile_launch_allowed(stale))
            self.assertEqual(os.readlink(lock), "host-4321")

            lock.unlink()
            lock.write_text("not-a-lock", encoding="utf-8")
            unknown = inspect_profile_lock(tmp, pid_alive=lambda pid: False)
            self.assertEqual(unknown["state"], "unknown")
            self.assertFalse(profile_launch_allowed(unknown))
            self.assertTrue(lock.is_file())

    def test_absent_lock_is_the_only_launchable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = inspect_profile_lock(tmp, pid_alive=lambda pid: True)
        self.assertEqual(info["state"], "absent")
        self.assertTrue(profile_launch_allowed(info))


class _Chromium:
    def __init__(self):
        self.launch_calls = 0

    async def launch_persistent_context(self, **kwargs):
        self.launch_calls += 1
        raise RuntimeError("launched")


class _Playwright:
    def __init__(self):
        self.chromium = _Chromium()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ProfileOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, tmp, alive):
        playwright = _Playwright()
        with (
            patch("bing_rewards.PROFILES_DIR", tmp),
            patch("bing_rewards.pid_is_alive", side_effect=alive),
            patch("bing_rewards.async_playwright", return_value=playwright),
            patch("bing_rewards.fetch_hot_words", new=AsyncMock(side_effect=AssertionError("network"))),
        ):
            status = await run_one_account({"name": "acct", "cookies": "A=1"}, 0, 1, local=False)
        return status, playwright

    async def test_live_lock_does_not_launch_or_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "acct"
            profile.mkdir()
            lock = profile / "SingletonLock"
            lock.symlink_to("host-4321")
            status, playwright = await self._run(tmp, lambda pid: True)
            self.assertEqual(status, "error")
            self.assertEqual(playwright.chromium.launch_calls, 0)
            self.assertTrue(lock.is_symlink())

    async def test_stale_lock_does_not_launch_or_get_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "acct"
            profile.mkdir()
            lock = profile / "SingletonLock"
            lock.symlink_to("host-4321")
            status, playwright = await self._run(tmp, lambda pid: False)
            self.assertEqual(status, "error")
            self.assertEqual(playwright.chromium.launch_calls, 0)
            self.assertEqual(os.readlink(lock), "host-4321")

    async def test_absent_lock_still_reaches_the_existing_launch_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            playwright = _Playwright()
            with (
                patch("bing_rewards.PROFILES_DIR", tmp),
                patch("bing_rewards.async_playwright", return_value=playwright),
                patch("bing_rewards.fetch_hot_words", new=AsyncMock(return_value=["word"])),
            ):
                with self.assertRaises(RuntimeError):
                    await run_one_account({"name": "acct", "cookies": "A=1"}, 0, 1, local=False)
            self.assertEqual(playwright.chromium.launch_calls, 1)


class NotificationTests(unittest.TestCase):
    def test_selective_invocation_does_not_notify_even_if_push_method_is_set(self):
        with patch.dict(os.environ, {"PUSH_METHOD": "pushplus", "PUSHPLUS_TOKEN": "x"}, clear=False):
            with (
                patch("bing_rewards.push_send") as send,
                patch("push.requests.post") as post,
                patch("push.requests.get") as get,
            ):
                self.assertFalse(emit_run_notification(True, False, "title", "body", False))
                send.assert_not_called()
                post.assert_not_called()
                get.assert_not_called()

    def test_empty_push_method_does_not_request(self):
        import push
        with patch.dict(os.environ, {"PUSH_METHOD": ""}, clear=False):
            with patch("push.requests.post") as post, patch("push.requests.get") as get:
                self.assertFalse(push.send("title", "body", False))
                post.assert_not_called()
                get.assert_not_called()

    def test_full_run_still_uses_the_push_entrypoint(self):
        with patch("bing_rewards.push_send", return_value=False) as send:
            self.assertFalse(emit_run_notification(False, False, "title", "body", True))
            send.assert_called_once_with("title", "body", is_success=True)

    def test_local_mode_does_not_notify(self):
        with patch("bing_rewards.push_send") as send:
            self.assertFalse(emit_run_notification(False, True, "title", "body", True))
            send.assert_not_called()


class SearchStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_partial_unchanged_unknown_and_hard_cap(self):
        calls = {"n": 0}

        async def no_search():
            calls["n"] += 1

        async def full():
            return DONE

        done = await run_refresh_bounded_searches(full, no_search)
        self.assertEqual(done["action"], "done")
        self.assertEqual(done["reason"], "remaining_zero")
        self.assertEqual(calls["n"], 0)

        async def partial():
            return PARTIAL

        stopped = await run_refresh_bounded_searches(partial, no_search)
        self.assertEqual(stopped["action"], "stop")
        self.assertEqual(stopped["reason"], "remaining_unproven")
        self.assertEqual(calls["n"], 0)
        self.assertFalse(stopped["complete"])

        sequence = [
            _proven(50),
            _proven(50),
        ]

        async def unchanged():
            return sequence.pop(0)

        unchanged_result = await run_refresh_bounded_searches(unchanged, no_search)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(unchanged_result["action"], "stop")
        self.assertEqual(unchanged_result["reason"], "unchanged")

        async def missing():
            return None

        unknown = await run_refresh_bounded_searches(missing, no_search)
        self.assertEqual(unknown["reason"], "remaining_unproven")
        self.assertEqual(calls["n"], 1)

        self.assertEqual(search_step_status(_proven(50), _proven(55)), "advanced")
        self.assertEqual(search_step_status(_proven(55), _proven(60, progress_max=60)), "complete")
        self.assertEqual(search_step_status(_proven(50), None), "unknown")
        self.assertEqual(search_step_status(_proven(40), _proven(50)), "inconsistent")

        capped = [_proven(0, progress_max=20, per_action=5), _proven(5, progress_max=20, per_action=5)]

        async def capped_refresh():
            if capped:
                return capped.pop(0)
            return _proven(5, progress_max=20, per_action=5)

        with patch("bing_rewards.MAX_SEARCHES", 1):
            hard = await run_refresh_bounded_searches(capped_refresh, no_search)
        self.assertEqual(hard["action"], "stop")
        self.assertEqual(hard["reason"], "hard_cap")
        self.assertEqual(hard["completed"], 1)
        self.assertFalse(hard["complete"])
        self.assertEqual(calls["n"], 2)

    async def test_proven_steps_stop_when_remaining_is_zero(self):
        snaps = [_proven(50), _proven(55), _proven(55), _proven(60)]
        calls = {"n": 0}

        async def refresh():
            return snaps.pop(0)

        async def search():
            calls["n"] += 1

        result = await run_refresh_bounded_searches(refresh, search)
        self.assertEqual(result["action"], "done")
        self.assertEqual(result["reason"], "refreshed_complete")
        self.assertEqual(calls["n"], 2)

    async def test_restriction_stops_the_search_loop(self):
        calls = {"n": 0}

        async def refresh():
            return _proven(40, activities=3)

        async def search():
            calls["n"] += 1
            raise AccountRestrictedError()

        with self.assertRaises(AccountRestrictedError):
            await run_refresh_bounded_searches(refresh, search)
        self.assertEqual(calls["n"], 1)


class _ReadingResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _ReadingClient:
    def __init__(self):
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        return _ReadingResponse({
            "response": {
                "promotions": [{
                    "name": "readarticle3_30points",
                    "attributes": {
                        "activityprogress": "0",
                        "activitymax": "1",
                        "complete": "False",
                    },
                }]
            }
        })

    async def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "body": json})
        return _ReadingResponse({
            "code": 0,
            "isDuplicate": False,
            "response": {"activity": {"p": 0}, "balance": 1},
        })


class ReadingImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_reading_path_has_hashlib_and_does_not_hit_network(self):
        client = _ReadingClient()
        page = QuietPage(url="https://rewards.bing.com/dashboard", title="Rewards")

        def client_factory(*args, **kwargs):
            return client

        with (
            patch("bing_rewards.httpx.AsyncClient", client_factory),
            patch("bing_rewards.asyncio.sleep", new=AsyncMock()),
        ):
            await perform_reading_task(page, "reader", {"value": "Bearer test-token"})

        self.assertEqual(len(client.posts), 1)
        body = client.posts[0]["body"]
        self.assertEqual(body["attributes"]["offerid"], "ENUS_readarticle3_30points")
        self.assertEqual(len(body["id"]), 64)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in body["id"]))
        self.assertIn("/dapi/me/activities", client.posts[0]["url"])
        self.assertNotIn("test-token", body["id"])


class ProductionBaselineTests(unittest.TestCase):
    def test_repair_does_not_depend_on_unprovided_edge_browsing_or_delete_locks(self):
        source_path = Path(__file__).resolve().parents[1] / "bing_rewards.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("edge_browsing", source)
        self.assertNotIn("os.remove", source)
        self.assertNotIn("os.unlink", source)
        self.assertNotIn("shutil.rmtree", source)
        digest = hashlib.sha256(source_path.with_name("browser_utils.py").read_bytes()).hexdigest()
        self.assertEqual(digest, "1fb5baa321975e040d7ad7405744c33c8630e12b3f4ec1f5464df17b73059178")
        digest = hashlib.sha256(source_path.with_name("login_helper.py").read_bytes()).hexdigest()
        self.assertEqual(digest, "82d5a773c576cf8dd16a394399cf70d522d0768ab1fa4f454bcb014ef686a2e4")
        digest = hashlib.sha256(source_path.with_name("push.py").read_bytes()).hexdigest()
        self.assertEqual(digest, "79ca65c8ac5592f27334d6c1d9609387085c6e167477b4110565583954390fe9")


if __name__ == "__main__":
    unittest.main()
