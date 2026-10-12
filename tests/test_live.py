import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot

OWNER, REQUESTER, STRANGER = 213925600, 555, 777


def link_message(user_id=REQUESTER):
    user = SimpleNamespace(id=user_id, full_name="Вася", username="vasya")
    chat = SimpleNamespace(id=-100, type=bot.ChatType.SUPERGROUP, title="Чат")
    return SimpleNamespace(chat=chat, from_user=user, sender_chat=None, bot=SimpleNamespace())


def status_message():
    return SimpleNamespace(
        edit_text=AsyncMock(), delete=AsyncMock(), chat=SimpleNamespace(type=bot.ChatType.SUPERGROUP)
    )


class LiveDetectionTests(unittest.TestCase):
    def test_live_stream_is_detected(self):
        with self.assertRaises(bot.LiveStreamError):
            bot.ensure_not_live({"live_status": "is_live", "title": "Эфир"})
        bot.ensure_not_live({"live_status": "was_live"})  # запись закончившегося эфира качаем как обычно
        bot.ensure_not_live({})

    def test_live_job_has_only_cancel_button(self):
        job = bot.Job(REQUESTER, "live")
        try:
            kb = bot.progress_kb(job)
            self.assertEqual([b.text for row in kb.inline_keyboard for b in row], ["❌ Отменить"])
        finally:
            job.close()


class LiveOfferTests(unittest.TestCase):
    def setUp(self):
        for name, value in {
            "live_offers": {},
            "ALLOWED_USER_IDS": {OWNER},
            "IGNORED_USER_IDS": set(),
            "pending_requests": 0,
            "pending_by_user": {},
        }.items():
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_live_link_gets_record_buttons_instead_of_long_wait(self):
        status = status_message()

        async def live(*args, **kwargs):
            raise bot.LiveStreamError({"title": "Эфир"})

        with patch.object(bot, "run_download", live), patch.object(bot, "report_failure", AsyncMock()) as report:
            asyncio.run(bot.download_and_send(link_message(), status, "https://youtu.be/x", "yt:x", "Вася", "video"))
        text = status.edit_text.call_args.args[0]
        kb = status.edit_text.call_args.kwargs["reply_markup"]
        self.assertIn("прямой эфир", text)
        self.assertEqual([b.text for b in kb.inline_keyboard[0]], ["⏺ 1 мин", "⏺ 5 мин", "⏺ 10 мин"])
        self.assertEqual(len(bot.live_offers), 1)
        report.assert_not_awaited()  # это не сбой — владельцу не пишем

    def press(self, user_id, minutes=5, offer_id=None):
        if offer_id is None:
            offer_id = bot.offer_live_clip(link_message(), "https://youtu.be/x", "yt:x", "Вася")
        query = SimpleNamespace(
            data=f"rec:{offer_id}:{minutes}",
            from_user=SimpleNamespace(id=user_id, username="u", full_name="u"),
            message=status_message(), answer=AsyncMock(),
        )
        with patch.object(bot, "download_and_send", AsyncMock()) as run:
            asyncio.run(bot.cb_record(query))
        return query, run

    def test_requester_starts_recording_once(self):
        query, run = self.press(REQUESTER, minutes=5)
        run.assert_awaited_once()
        self.assertEqual(run.call_args.args[5], "live")
        self.assertEqual(run.call_args.kwargs["live_seconds"], 300)
        self.assertEqual(bot.live_offers, {})  # кнопка одноразовая
        self.assertEqual(bot.pending_requests, 0)  # место в очереди вернули

    def test_stranger_and_stale_offer_are_refused(self):
        query, run = self.press(STRANGER)
        run.assert_not_awaited()
        self.assertIn("только тот", query.answer.call_args.args[0])

        query, run = self.press(REQUESTER, offer_id="999")
        run.assert_not_awaited()
        self.assertIn("устарело", query.answer.call_args.args[0])

    def test_owner_may_record_and_odd_minutes_are_refused(self):
        _, run = self.press(OWNER, minutes=10)
        run.assert_awaited_once()
        query, run = self.press(REQUESTER, minutes=60)
        run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
