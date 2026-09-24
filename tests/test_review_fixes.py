import asyncio
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class UrlParsingTests(unittest.TestCase):
    def test_trailing_punctuation_is_not_part_of_link(self):
        text = "глянь (https://youtu.be/abc123), и ещё «https://youtu.be/xyz789»."
        self.assertEqual(bot.find_urls(text), ["https://youtu.be/abc123", "https://youtu.be/xyz789"])

    def test_instagram_post_forms_share_one_key(self):
        forms = [
            "https://www.instagram.com/p/DdMYiWbFmv0/",
            "https://www.instagram.com/reel/DdMYiWbFmv0/?igsh=abc",
            "https://www.instagram.com/reels/DdMYiWbFmv0",
            "https://www.instagram.com/someone/p/DdMYiWbFmv0/",
        ]
        self.assertEqual({bot.url_key(u) for u in forms}, {"ig:reel:DdMYiWbFmv0"})

    def test_stories_keep_full_path_key(self):
        story = "https://www.instagram.com/stories/someone/3993438272162923132"
        self.assertEqual(bot.url_key(story), "ig:stories:someone:3993438272162923132")


class CaptionTests(unittest.TestCase):
    def test_escaped_caption_fits_telegram_limit(self):
        title = "Tom & Jerry <3 " * 80  # ~1200 символов до экранирования, «&» и «<» раздуваются
        caption = bot.tg_caption(title[:900])
        self.assertLessEqual(len(caption), bot.CAPTION_LIMIT)
        self.assertFalse(caption.endswith("&am"))  # не разрезали сущность

    def test_short_caption_untouched(self):
        self.assertEqual(bot.tg_caption("Tom & Jerry"), "Tom &amp; Jerry")
        self.assertIsNone(bot.tg_caption(""))


class MoovTests(unittest.TestCase):
    def test_truncated_large_box_header_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cut.mp4"
            path.write_bytes(struct.pack(">I4s", 1, b"free") + bytes([0, 0, 1]))  # 64-битный размер обрезан
            self.assertFalse(bot._moov_first(path))


class TimeoutTests(unittest.TestCase):
    def test_timeout_waits_for_thread_before_returning(self):
        finished = threading.Event()

        def slow(hook):
            # поток, который не сразу замечает отмену — как yt-dlp во время склейки
            for _ in range(20):
                time.sleep(0.05)
                try:
                    hook()
                except bot.CancelledByUser:
                    break
            finished.set()

        async def scenario():
            job = bot.Job(1, "video")
            status = SimpleNamespace(edit_text=AsyncMock(), chat=SimpleNamespace(type=bot.ChatType.PRIVATE))
            with patch.object(bot, "DOWNLOAD_TIMEOUT", 0.1):
                with self.assertRaises(bot.DownloadTimedOut):
                    await bot.run_download(job, status, slow, job.check)
            job.close()
            return status

        status = asyncio.run(scenario())
        self.assertTrue(finished.is_set(), "run_download вернулся, пока поток ещё работал")
        status.edit_text.assert_awaited()  # пользователю сообщили о таймауте


class CachedAlbumTests(unittest.TestCase):
    def test_partial_album_from_cache_is_not_redownloaded(self):
        async def scenario():
            album = {"ig:reel:X": {"album": [["photo", "a"]] * 12, "title": "t"}}
            with (
                patch.object(bot, "file_ids", album),
                patch.object(bot, "save_cache"),
                patch.object(bot, "show_error", AsyncMock()),
            ):
                async def half_sent(message, items, caption, sent_ids=None):
                    sent_ids.extend(items[:10])
                    raise RuntimeError("flood wait")

                with patch.object(bot, "send_album", half_sent):
                    ok = await bot.send_cached(SimpleNamespace(), SimpleNamespace(), "ig:reel:X", "video")
                return ok, dict(bot.file_ids)

        ok, cache = asyncio.run(scenario())
        self.assertTrue(ok)
        self.assertIn("ig:reel:X", cache)


class DeletedLinkMessageTests(unittest.TestCase):
    def test_replies_survive_deleted_original(self):
        from aiogram import Bot
        from aiogram.types import Message as TgMessage

        tg = Bot("1:" + "a" * 35, default=bot.BOT_DEFAULTS)
        msg = TgMessage.model_validate(
            {"message_id": 5, "date": 0, "chat": {"id": -100, "type": "supergroup"}}, context={"bot": tg}
        )
        params = tg.session.prepare_value(msg.reply_video("file").reply_parameters, bot=tg, files={})
        self.assertIn('"allow_sending_without_reply": true', params)


if __name__ == "__main__":
    unittest.main()
