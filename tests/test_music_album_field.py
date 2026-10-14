import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot


class MusicAlbumFieldTests(unittest.TestCase):
    def test_track_album_name_is_not_taken_for_photo_album(self):
        # yt-dlp пишет в info["album"] название альбома трека — у музыкальных клипов YouTube и TikTok
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "v.mp4"
            video.write_bytes(b"x" * 10)

            async def grabbed(*args, **kwargs):
                return video, {"title": "Клип", "album": "Greatest Hits", "height": 1080}

            sent = SimpleNamespace(video=SimpleNamespace(file_id="F"), audio=None)
            message = SimpleNamespace(
                chat=SimpleNamespace(id=-100, type=bot.ChatType.SUPERGROUP, title="Чат"),
                from_user=SimpleNamespace(id=555, full_name="Вася", username="vasya"), sender_chat=None,
                bot=AsyncMock(return_value=sent), reply_video=Mock(return_value="send"),
            )
            message.bot.send_chat_action = AsyncMock()
            status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock(),
                                     chat=SimpleNamespace(type=bot.ChatType.SUPERGROUP))
            with (
                patch.object(bot, "run_download", grabbed),
                patch.object(bot, "send_album", AsyncMock()) as album,
                patch.object(bot, "cache_put") as cache,
                patch.object(bot, "report_failure", AsyncMock()) as report,
            ):
                asyncio.run(bot.download_and_send(message, status, "https://youtu.be/x", "yt:x", "Вася", "video"))
        album.assert_not_awaited()
        message.reply_video.assert_called_once()
        cache.assert_called_once()
        report.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
