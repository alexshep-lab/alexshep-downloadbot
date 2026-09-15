import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot


def message(user_id: int, chat_id: int):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        sender_chat=None,
        chat=SimpleNamespace(id=chat_id),
    )


class AccessControlTests(unittest.TestCase):
    def test_empty_allowlists_are_fail_closed(self):
        with (
            patch.object(bot, "ALLOWED_USER_IDS", set()),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
        ):
            self.assertFalse(bot.is_allowed(message(10, -20)))

    def test_user_or_chat_allowlist_grants_access(self):
        with (
            patch.object(bot, "ALLOWED_USER_IDS", {10}),
            patch.object(bot, "ALLOWED_CHAT_IDS", {-20}),
        ):
            self.assertTrue(bot.is_allowed(message(10, -99)))
            self.assertTrue(bot.is_allowed(message(99, -20)))
            self.assertFalse(bot.is_allowed(message(99, -99)))


class UrlValidationTests(unittest.TestCase):
    def test_only_https_supported_hosts_are_allowed(self):
        self.assertTrue(bot.is_supported("https://youtube.com/watch?v=abc"))
        self.assertTrue(bot.is_supported("https://www.instagram.com/reel/abc/"))
        self.assertFalse(bot.is_supported("http://youtube.com/watch?v=abc"))
        self.assertFalse(bot.is_supported("https://youtube.com.evil.test/watch?v=abc"))
        self.assertFalse(bot.is_supported("https://youtube.com@evil.test/watch?v=abc"))
        self.assertFalse(bot.is_supported("https://youtube.com:444/watch?v=abc"))
        self.assertFalse(bot.is_supported("https://127.0.0.1/video"))

    def test_log_url_drops_credentials_query_and_fragment(self):
        value = bot.safe_url_for_log("https://name:secret@youtube.com/watch?token=secret#part")
        self.assertEqual(value, "https://youtube.com/watch")
        self.assertNotIn("secret", value)

    def test_fallback_cache_key_does_not_store_query_secret(self):
        value = bot.url_key("https://youtube.com/custom/path?share_token=secret")
        self.assertTrue(value.startswith("yt:url:"))
        self.assertNotIn("secret", value)

    def test_cache_key_cannot_inject_log_lines(self):
        value = bot.url_key("https://youtube.com/watch?v=abc%0aFAKE-LOG")
        self.assertNotIn("\n", value)
        self.assertNotIn("FAKE-LOG", value)


class DownloadLimitTests(unittest.TestCase):
    class Response:
        def __init__(self, content: bytes, content_length: str | None = None):
            self._stream = io.BytesIO(content)
            self.headers = {"Content-Length": content_length} if content_length else {}

        def read(self, size: int) -> bytes:
            return self._stream.read(size)

    def test_stream_is_stopped_at_limit(self):
        target = io.BytesIO()
        with self.assertRaises(bot.TooLargeError):
            bot._copy_limited(self.Response(b"123456"), target, 5)

    def test_content_length_is_checked_before_copy(self):
        target = io.BytesIO()
        with self.assertRaises(bot.TooLargeError):
            bot._copy_limited(self.Response(b"", "100"), target, 5)
        self.assertEqual(target.getvalue(), b"")


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.pending_requests = 0
        bot.pending_by_user.clear()
        bot.request_state_lock = asyncio.Lock()

    async def asyncTearDown(self):
        bot.pending_requests = 0
        bot.pending_by_user.clear()

    async def test_global_and_per_user_limits(self):
        with (
            patch.object(bot, "MAX_PENDING_REQUESTS", 2),
            patch.object(bot, "MAX_REQUESTS_PER_USER", 1),
        ):
            self.assertIsNone(await bot.reserve_request(1))
            self.assertEqual(await bot.reserve_request(1), "user")
            self.assertIsNone(await bot.reserve_request(2))
            self.assertEqual(await bot.reserve_request(3), "global")
            await bot.release_request(1)
            self.assertIsNone(await bot.reserve_request(3))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(bot.file_ids)
        bot.file_ids.clear()

    def tearDown(self):
        bot.file_ids.clear()
        bot.file_ids.update(self.original)

    def test_oldest_entries_are_trimmed(self):
        bot.file_ids.update({"first": {}, "second": {}, "third": {}})
        with patch.object(bot, "MAX_CACHE_ENTRIES", 2):
            self.assertTrue(bot.trim_cache())
        self.assertEqual(list(bot.file_ids), ["second", "third"])


class UrlLockTests(unittest.TestCase):
    def tearDown(self):
        bot.url_locks.clear()

    def test_lock_is_removed_after_last_waiter(self):
        first = bot.retain_url_lock("yt:abc")
        second = bot.retain_url_lock("yt:abc")
        self.assertIs(first, second)
        bot.release_url_lock("yt:abc", first)
        self.assertIn("yt:abc", bot.url_locks)
        bot.release_url_lock("yt:abc", second)
        self.assertNotIn("yt:abc", bot.url_locks)


class ConfigurationTests(unittest.TestCase):
    def test_empty_allowlist_is_rejected_at_startup(self):
        with (
            patch.object(bot, "BOT_TOKEN", "token"),
            patch.object(bot, "ALLOWED_USER_IDS", set()),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
        ):
            with self.assertRaises(SystemExit):
                bot.validate_security_config()

    def test_remote_bot_api_is_rejected(self):
        with (
            patch.object(bot, "BOT_TOKEN", "token"),
            patch.object(bot, "ALLOWED_USER_IDS", {1}),
            patch.object(bot, "ALLOWED_CHAT_IDS", set()),
            patch.object(bot, "COOKIES_FILE", ""),
            patch.object(bot, "TELEGRAM_API_URL", "http://192.168.1.10:8081"),
        ):
            with self.assertRaises(SystemExit):
                bot.validate_security_config()


if __name__ == "__main__":
    unittest.main()
