import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp

import bot

URL = "https://x.com/someone/status/2109579203373105357"


def tweet(*, photos=(), quoted_photos=(), text="Смотри https://t.co/abc"):
    def media(urls):
        return {"media": [{"type": "photo", "media_url_https": u} for u in urls]}

    status = {"full_text": text, "user": {"screen_name": "someone", "name": "Кто-то"}}
    if photos:
        status["extended_entities"] = media(photos)
    if quoted_photos:
        status["quoted_status"] = {"extended_entities": media(quoted_photos)}
    return status


class FakeYDL:
    status = {}

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_info_extractor(self, name):
        assert name == "Twitter"
        status = self.status

        class IE:
            def initialize(self):
                pass

            def _extract_status(self, twid):
                assert twid == "2109579203373105357"
                return status

        return IE()


def fake_fetch(ydl, url, dest, headers=None, max_bytes=0, source=""):
    path = dest.with_suffix(".jpg")
    path.write_bytes(url.encode())
    return path


class XPhotoTests(unittest.TestCase):
    def grab(self, status):
        FakeYDL.status = status
        workdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch.object(bot.yt_dlp, "YoutubeDL", FakeYDL), patch.object(bot, "_ig_fetch", fake_fetch):
            return bot.x_photos(URL, workdir)

    def test_photos_come_as_album_with_tweet_text(self):
        _, info = self.grab(tweet(photos=["https://pbs.twimg.com/media/A.jpg", "https://pbs.twimg.com/media/B.jpg"]))
        self.assertEqual([kind for kind, _ in info["album"]], ["photo", "photo"])
        self.assertIn("name=large", info["album"][0][1].read_text())
        self.assertEqual(info["title"], "@someone: Смотри")  # ссылка t.co из текста убрана

    def test_quoted_tweet_photos_are_used_when_tweet_has_none(self):
        _, info = self.grab(tweet(quoted_photos=["https://pbs.twimg.com/media/Q.jpg"]))
        self.assertEqual(len(info["album"]), 1)

    def test_hidden_tweet_is_not_called_empty(self):
        # так graphql-заглушку TweetTombstone превращает yt-dlp
        hidden = {"card": None, "quoted_status": None, "retweeted_status": None, "user": None}
        with self.assertRaises(yt_dlp.utils.DownloadError) as ctx:
            self.grab(hidden)
        self.assertIn("только залогиненным", bot.friendly_dlp_error(ctx.exception, "x"))

    def test_old_http_tco_links_are_stripped(self):
        _, info = self.grab(tweet(photos=["https://pbs.twimg.com/media/A.jpg"], text="Four more years. http://t.co/x"))
        self.assertEqual(info["title"], "@someone: Four more years.")

    def test_text_only_tweet_says_so(self):
        with self.assertRaises(yt_dlp.utils.DownloadError) as ctx:
            self.grab(tweet())
        self.assertIn("ни картинок", bot.friendly_dlp_error(ctx.exception, "x"))


class XRoutingTests(unittest.TestCase):
    def test_no_video_tweet_falls_back_to_photos(self):
        def no_video(*args):
            raise yt_dlp.utils.DownloadError("ERROR: [twitter] 1: No video could be found in this tweet")

        album = (Path("."), {"album": [("photo", Path("a.jpg"))], "title": "t"})
        with patch.object(bot, "_download_video", no_video), patch.object(bot, "x_photos", return_value=album) as photos:
            self.assertIs(bot.download_video(URL, Path("w")), album)
        photos.assert_called_once()

    def test_other_errors_are_not_masked(self):
        def broken(*args):
            raise yt_dlp.utils.DownloadError("ERROR: HTTP Error 404: Not Found")

        with patch.object(bot, "_download_video", broken), patch.object(bot, "x_photos") as photos:
            with self.assertRaises(yt_dlp.utils.DownloadError):
                bot.download_video(URL, Path("w"))
        photos.assert_not_called()

    def test_audio_of_photo_tweet_explains(self):
        exc = yt_dlp.utils.DownloadError("ERROR: [twitter] 1: No video could be found in this tweet")
        self.assertIn("звук взять неоткуда", bot.friendly_dlp_error(exc, "x"))


if __name__ == "__main__":
    unittest.main()
