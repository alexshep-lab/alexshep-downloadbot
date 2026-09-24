import asyncio
import collections
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot

OWNER = 213925600
GROUP = -100777


def group_message(chat_id=GROUP, text="/logs"):
    user = SimpleNamespace(id=555, full_name="Вася", username="vasya")
    chat = SimpleNamespace(id=chat_id, type=bot.ChatType.SUPERGROUP, title="Чат")
    tg = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(chat=chat, from_user=user, sender_chat=None, text=text, bot=tg,
                           reply=AsyncMock(), answer=AsyncMock())


class ErrorReportTests(unittest.TestCase):
    def setUp(self):
        for name, value in {
            "ALLOWED_USER_IDS": {OWNER},
            "OWNER_ERROR_REPORTS": True,
            "error_report_last": {},
            "error_report_times": collections.deque(),
        }.items():
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)

    def report(self, msg, detail="ERROR: HTTP Error 403: Forbidden", key="yt:abc"):
        asyncio.run(bot.report_failure(msg, key, "https://youtu.be/abc?si=secret", "video", "ошибка загрузки", detail))

    def test_owner_gets_report_without_url_token(self):
        msg = group_message()
        self.report(msg)
        text = msg.bot.send_message.call_args.args[1]
        self.assertEqual(msg.bot.send_message.call_args.args[0], OWNER)
        self.assertIn("yt:abc", text)
        self.assertNotIn("secret", text)

    def test_same_error_is_not_repeated_but_different_one_is(self):
        msg = group_message()
        self.report(msg, key="yt:one")
        self.report(msg, key="yt:two")  # та же поломка на другом ролике — не повторяем
        self.report(msg, detail="ERROR: TikTok changed something", key="tt:x")
        self.assertEqual(msg.bot.send_message.await_count, 2)

    def test_hourly_cap(self):
        msg = group_message()
        with patch.object(bot, "MAX_ERROR_REPORTS_PER_HOUR", 3):
            for n in range(6):
                self.report(msg, detail=f"ERROR: different failure {'x' * n}")
        self.assertEqual(msg.bot.send_message.await_count, 3)

    def test_owner_private_chat_is_not_duplicated(self):
        msg = group_message(chat_id=OWNER)
        msg.chat.type = bot.ChatType.PRIVATE
        self.report(msg)
        msg.bot.send_message.assert_not_awaited()

    def test_can_be_disabled(self):
        msg = group_message()
        with patch.object(bot, "OWNER_ERROR_REPORTS", False):
            self.report(msg)
        msg.bot.send_message.assert_not_awaited()


class RecentLogsTests(unittest.TestCase):
    def make(self):
        handler = bot.RecentLogs(capacity=50)
        own = logging.getLogger(bot.log.name)
        other = logging.getLogger("aiogram.event")
        return handler, own, other

    def emit(self, handler, logger, level, text):
        handler.emit(logger.makeRecord(logger.name, level, __file__, 1, text, (), None))

    def test_filters_library_noise_and_splits_levels(self):
        handler, own, other = self.make()
        self.emit(handler, other, logging.INFO, "Update id=1 is handled")
        self.emit(handler, own, logging.INFO, "запрос: ссылка")
        self.emit(handler, own, logging.WARNING, "yt-dlp error <b>")
        self.emit(handler, other, logging.ERROR, "aiogram упал")
        errors = handler.tail(everything=False)
        everything = handler.tail(everything=True)
        self.assertNotIn("Update id", everything)
        self.assertIn("запрос: ссылка", everything)
        self.assertNotIn("запрос: ссылка", errors)
        self.assertIn("yt-dlp error", errors)
        self.assertIn("aiogram упал", errors)

    def test_tail_fits_telegram_message(self):
        handler, own, _ = self.make()
        for n in range(50):
            self.emit(handler, own, logging.WARNING, f"ошибка {n} " + "x" * 300)
        text = handler.tail(everything=False)
        self.assertLessEqual(len(text), 3800)
        self.assertTrue(text.rstrip().endswith("x"))
        self.assertIn("ошибка 49", text)  # самые свежие — снизу и не отрезаны


class LogsCommandTests(unittest.TestCase):
    def test_only_owner_in_private(self):
        with patch.object(bot, "ALLOWED_USER_IDS", {OWNER}):
            stranger = group_message()
            stranger.chat.type = bot.ChatType.PRIVATE
            asyncio.run(bot.cmd_logs(stranger))
            stranger.answer.assert_not_awaited()

            owner_in_group = group_message()
            owner_in_group.from_user.id = OWNER
            asyncio.run(bot.cmd_logs(owner_in_group))
            self.assertIn("только в личке", owner_in_group.reply.call_args.args[0])

            owner = group_message(chat_id=OWNER)
            owner.from_user.id = OWNER
            owner.chat.type = bot.ChatType.PRIVATE
            with patch.object(bot, "recent_logs", bot.RecentLogs()):
                bot.recent_logs.records.append((logging.WARNING, "01.01 00:00:00 WARNING a<b>"))
                asyncio.run(bot.cmd_logs(owner))
            sent = owner.answer.call_args.args[0]
            self.assertIn("a&lt;b&gt;", sent)  # содержимое журнала экранировано


if __name__ == "__main__":
    unittest.main()
