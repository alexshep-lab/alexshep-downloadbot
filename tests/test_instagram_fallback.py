import asyncio
import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import bot


URL = "https://www.instagram.com/reel/DbjCzdzhoJO/"


class InstagramFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cookies = self.root / "cookies.txt"
        self.write_session("test-session-one")
        overrides = {
            "COOKIES_FILE": str(self.cookies),
            "COOKIES_SERVICES": {"ig"},
            "COOKIES_FALLBACK_SERVICES": {"yt"},
            "IG_STATE_FILE": self.root / "instagram_session_state.json",
            "ig_block_loaded": False,
            "ig_blocked_session": None,
            "ig_session_state": None,
        }
        for name, value in overrides.items():
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_session(self, value):
        self.cookies.write_text(
            "# Netscape HTTP Cookie File\n"
            f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{value}\n",
            encoding="utf-8",
        )

    def test_anonymous_success_does_not_use_account_even_with_all_services(self):
        def grab(url, workdir, hook, with_cookies):
            self.assertFalse(with_cookies)
            self.assertNotIn("cookiefile", bot._base_opts("ig", with_cookies))
            return self.root / "video.mp4", {}

        with patch.object(bot, "COOKIES_SERVICES", {"all"}), patch.object(bot, "ig_checkpoint") as probe:
            path, info = bot._with_photo_fallback(grab, Mock(), URL, self.root, None)
        self.assertEqual(path.name, "video.mp4")
        self.assertNotIn("_ig_authenticated", info)
        probe.assert_not_called()

    def test_login_failure_retries_with_cookies_and_marks_authenticated_success(self):
        calls = []

        def grab(url, workdir, hook, with_cookies):
            calls.append(with_cookies)
            if not with_cookies:
                raise bot.yt_dlp.utils.DownloadError("login required")
            self.assertIsInstance(bot._base_opts("ig", True)["cookiefile"], io.StringIO)
            return self.root, {}

        _, info = bot._with_cookie_retry(grab, URL, self.root, None)
        self.assertEqual(calls, [False, True])
        self.assertEqual(info["_ig_authenticated"], bot.ig_cookie_fingerprint())

    def test_checkpoint_disables_followup_requests_and_survives_reload(self):
        fn = Mock(side_effect=bot.yt_dlp.utils.DownloadError("checkpoint_required"))
        for _ in range(2):
            with self.assertRaises(bot.InstagramSessionUnavailable):
                bot.run_ig_authenticated(fn, URL)
        fn.assert_called_once()
        state = bot.IG_STATE_FILE.read_text()
        self.assertNotIn("test-session-one", state)
        bot.ig_block_loaded, bot.ig_blocked_session = False, None
        self.assertFalse(bot.ig_auth_available())

    def test_only_new_session_reenables_auth(self):
        bot.block_ig_session(bot.ig_cookie_fingerprint())
        self.cookies.write_text(self.cookies.read_text() + "# metadata changed\n")
        self.assertFalse(bot.ig_auth_available())
        self.write_session("test-session-two")
        self.assertTrue(bot.ig_auth_available())

    def test_400_is_verified_only_after_authenticated_failure(self):
        fn = Mock(side_effect=bot.yt_dlp.utils.DownloadError("HTTP Error 400: Bad Request"))
        with patch.object(bot, "ig_checkpoint", return_value=True) as probe:
            with self.assertRaises(bot.InstagramSessionUnavailable):
                bot._with_cookie_retry(fn, URL, self.root, None)
        self.assertEqual([call.args[-1] for call in fn.call_args_list], [False, True])
        probe.assert_called_once_with(URL)
        self.assertFalse(bot.ig_auth_available())

    def test_blocked_session_does_not_probe_again_on_anonymous_error(self):
        bot.block_ig_session(bot.ig_cookie_fingerprint())
        fn = Mock(side_effect=bot.yt_dlp.utils.DownloadError("HTTP Error 400: Bad Request"))
        with patch.object(bot, "ig_checkpoint") as probe:
            with self.assertRaises(bot.InstagramSessionUnavailable):
                bot._with_cookie_retry(fn, URL, self.root, None)
        fn.assert_called_once_with(URL, self.root, None, False)
        probe.assert_not_called()

    def test_blocked_photo_fallback_never_calls_authenticated_api(self):
        bot.block_ig_session(bot.ig_cookie_fingerprint())
        grab = Mock(side_effect=bot.yt_dlp.utils.DownloadError("There is no video in this post"))
        photos = Mock()
        with self.assertRaises(bot.InstagramSessionUnavailable):
            bot._with_photo_fallback(grab, photos, URL, self.root, None)
        photos.assert_not_called()

    def test_photo_fallback_marks_authenticated_success(self):
        grab = Mock(side_effect=bot.yt_dlp.utils.DownloadError("There is no video in this post"))
        photos = Mock(return_value=(self.root, {"album": []}))
        _, info = bot._with_photo_fallback(grab, photos, URL, self.root, None)
        photos.assert_called_once()
        self.assertEqual(info["_ig_authenticated"], bot.ig_cookie_fingerprint())

    def test_parallel_auth_jobs_stop_after_first_checkpoint(self):
        fn = Mock(side_effect=bot.yt_dlp.utils.DownloadError("checkpoint_required"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(bot.run_ig_authenticated, fn, URL) for _ in range(2)]
            for future in futures:
                with self.assertRaises(bot.InstagramSessionUnavailable):
                    future.result(timeout=5)
        fn.assert_called_once()

    def test_ytdlp_does_not_overwrite_exported_cookie_file(self):
        before = self.cookies.read_bytes()
        with bot.yt_dlp.YoutubeDL(bot._base_opts("ig", True)) as ydl:
            self.assertTrue(list(ydl.cookiejar))
            ydl.cookiejar.clear()
        self.assertEqual(self.cookies.read_bytes(), before)

    def test_anonymous_success_does_not_report_account_recovery(self):
        bot.block_ig_session(bot.ig_cookie_fingerprint())
        bot.ig_session_state = "checkpoint"
        with patch.object(bot, "notify_owners", new_callable=AsyncMock) as notify:
            asyncio.run(bot.report_ig_result(Mock(), {}))
        self.assertEqual(bot.ig_session_state, "checkpoint")
        notify.assert_not_awaited()

    def test_unverified_cookie_is_ready_not_ok(self):
        with patch.object(bot, "notify_owners", new_callable=AsyncMock) as notify:
            asyncio.run(bot.check_ig_session(Mock()))
        self.assertEqual(bot.ig_session_state, "ready")
        notify.assert_not_awaited()

    def test_rate_limit_does_not_trigger_account_retry(self):
        fn = Mock(side_effect=bot.yt_dlp.utils.DownloadError("HTTP 429 login required"))
        with self.assertRaises(bot.yt_dlp.utils.DownloadError):
            bot._with_cookie_retry(fn, URL, self.root, None)
        fn.assert_called_once_with(URL, self.root, None, False)

    def test_youtube_cookie_policy_is_preserved(self):
        self.assertNotIn("cookiefile", bot._base_opts("yt"))
        self.assertEqual(bot._base_opts("yt", True)["cookiefile"], str(self.cookies))


if __name__ == "__main__":
    unittest.main()


class LoggedOutSessionTests(unittest.TestCase):
    """Instagram может завершить сессию, оставив sessionid в файле: API тогда отвечает гостевым 404."""

    # то же окружение с cookies, но без повторного прогона всех тестов InstagramFallbackTests
    setUp = InstagramFallbackTests.setUp
    write_session = InstagramFallbackTests.write_session

    def test_logged_out_api_page_disables_session_instead_of_404(self):
        photo_url = "https://www.instagram.com/p/DcYnX0LqASx/"

        def grab(url, workdir, hook, with_cookies):
            raise bot.yt_dlp.utils.DownloadError("ERROR: [Instagram] x: There is no video in this post")

        def fallback(url, workdir, hook):
            raise bot.yt_dlp.utils.DownloadError(f"Instagram: {bot.IG_LOGGED_OUT_MARKER}")

        with self.assertRaises(bot.InstagramSessionUnavailable) as caught:
            bot._with_photo_fallback(grab, fallback, photo_url, self.root, None)
        self.assertFalse(bot.ig_auth_available())
        text = bot.friendly_dlp_error(caught.exception, "ig")
        self.assertNotIn("404", text)
        self.assertIn("сессия Instagram", text)


class ErrorTextTests(unittest.TestCase):
    def test_truncated_cdn_response_is_retried(self):
        exc = bot.yt_dlp.utils.DownloadError(
            "ERROR: [download] Got error: 4922 bytes read, 10058599 more expected. Giving up after 3 retries"
        )
        self.assertTrue(bot.is_transient_dlp_error(exc))

    def test_tiktok_challenge_and_future_live_have_clear_messages(self):
        tiktok = bot.yt_dlp.utils.DownloadError(
            "ERROR: [TikTok] 1: Unexpected response from webpage request; please report this issue"
        )
        self.assertIn("TikTok", bot.friendly_dlp_error(tiktok, "tt"))
        live = bot.yt_dlp.utils.DownloadError("ERROR: [youtube] x: This live event will begin in 63 minutes.")
        self.assertIn("ещё не началась", bot.friendly_dlp_error(live, "yt"))


class AudienceRestrictedTests(unittest.TestCase):
    """Пост с возрастным/страновым ограничением: анонимно нельзя, надо пробовать с аккаунтом."""

    setUp = InstagramFallbackTests.setUp
    write_session = InstagramFallbackTests.write_session

    def test_restricted_post_is_retried_with_account(self):
        attempts = []

        def grab(url, workdir, hook, with_cookies):
            attempts.append(with_cookies)
            if not with_cookies:
                raise bot.yt_dlp.utils.DownloadError(
                    "ERROR: [Instagram] x: This content isn't available to everyone: "
                    "It can't be seen by certain audiences."
                )
            return self.root / "video.mp4", {}

        path, info = bot._with_cookie_retry(grab, URL, self.root, None)
        self.assertEqual(attempts, [False, True])
        self.assertEqual(path.name, "video.mp4")
        self.assertIn("_ig_authenticated", info)

    def test_message_explains_age_restriction(self):
        exc = bot.yt_dlp.utils.DownloadError(
            "ERROR: [Instagram] x: This content isn't available to everyone: It can't be seen by certain audiences."
        )
        text = bot.friendly_dlp_error(exc, "ig")
        self.assertIn("возрастное", text)
