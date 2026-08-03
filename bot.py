"""AlexShepDownloadBot — Telegram-бот для скачивания видео из YouTube и Instagram.

Запуск: python bot.py (настройки берутся из .env, см. .env.example).
"""

import asyncio
import html
import itertools
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(part) for part in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if part
}
ALLOWED_CHAT_IDS = {
    int(part) for part in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if part
}
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
# Для каких сервисов подставлять cookies: ig/yt/tt/x через запятую либо all.
# По умолчанию только Instagram: yt-dlp дописывает в файл cookies всех посещённых
# сайтов, и YouTube на своих же протухших cookies начинает отвечать 403.
COOKIES_SERVICES = {
    s for s in os.getenv("COOKIES_SERVICES", "ig").replace(" ", "").lower().split(",") if s
}
PROXY = os.getenv("PROXY", "").strip()
ALLOW_ANY_SITE = os.getenv("ALLOW_ANY_SITE", "").strip().lower() in {"1", "true", "yes"}
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR") or Path(tempfile.gettempdir()) / "alexshep_download_bot")
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "900"))
UPLOAD_TIMEOUT = int(os.getenv("UPLOAD_TIMEOUT", "600"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
# адрес локального Bot API server (например http://127.0.0.1:8081); пусто = облачный Telegram
TELEGRAM_API_URL = os.getenv("TELEGRAM_API_URL", "").strip()
# 50 МБ у облачного Bot API, до 2000 МБ у локального
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000" if TELEGRAM_API_URL else "50"))
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "1080"))
# для длинных/тяжёлых видео 1080p не нужен — качаем в этом качестве
PREFERRED_HEIGHT = int(os.getenv("PREFERRED_HEIGHT", "720"))
HD_MAX_SIZE_MB = int(os.getenv("HD_MAX_SIZE_MB", "100"))  # тяжелее — уже не HD
HD_MAX_DURATION_MIN = int(os.getenv("HD_MAX_DURATION_MIN", "20"))  # длиннее — уже не HD
# как часто обновлять статус скачивания, сек (чаще 3 с Telegram начнёт ругаться)
PROGRESS_INTERVAL = float(os.getenv("PROGRESS_INTERVAL", "5"))
# если скорость просела ниже — соединение придушено, берём свежую ссылку и продолжаем
THROTTLE_FLOOR_KB = int(os.getenv("THROTTLE_FLOOR_KB", "500"))
# поймали 429 — на это время перестаём дёргать сервис, иначе блокировка только продлевается
RATE_LIMIT_COOLDOWN_MIN = int(os.getenv("RATE_LIMIT_COOLDOWN_MIN", "30"))
# в группах сообщения об ошибках самоудаляются через столько секунд (0 = висят всегда)
ERROR_TTL_SEC = int(os.getenv("ERROR_TTL_SEC", "60"))

DOWNLOAD_ATTEMPTS = 3  # сколько раз пробовать при временных ошибках (403 и т.п.)
RETRY_DELAYS = (15, 45)  # паузы перед 2-й и 3-й попыткой, сек

MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
SIZE_TARGET = int(MAX_FILE_SIZE * 0.96)  # целимся с запасом под лимит
HEIGHT_LADDER = tuple(h for h in (2160, 1440, 1080, 720, 480, 360) if h <= MAX_HEIGHT) or (360,)

URL_RE = re.compile(r"https?://\S+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")  # yt-dlp вставляет цветовые коды консоли в текст ошибок
SUPPORTED_HOSTS = (
    "youtube.com", "youtu.be", "instagram.com", "instagr.am",
    "x.com", "twitter.com", "tiktok.com",
)

log = logging.getLogger("downloadbot")
router = Router()
download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# одно и то же видео качаем один раз: второй чат ждёт на замке и получает готовый file_id
CACHE_FILE = Path(os.getenv("CACHE_FILE") or "file_ids.json")
file_ids: dict[str, dict] = {}  # ключ → {"file_id": …, "title": …}
url_locks: dict[str, asyncio.Lock] = {}
jobs: dict[str, "Job"] = {}  # идущие закачки, чтобы их можно было отменить кнопкой
job_counter = itertools.count(1)


class CancelledByUser(Exception):
    """Пользователь нажал «Отменить»."""


class Job:
    """Идущая закачка: нужна, чтобы кнопка могла её остановить."""

    def __init__(self, requester_id: int, kind: str) -> None:
        self.id = str(next(job_counter))
        self.requester_id = requester_id
        self.kind = kind  # video | audio
        self.cancelled = False
        jobs[self.id] = self

    def check(self) -> None:
        if self.cancelled:
            raise CancelledByUser

    def close(self) -> None:
        jobs.pop(self.id, None)


def may_manage(user_id: int, requester_id: int) -> bool:
    """Управлять закачкой может тот, кто её заказал, и владелец бота."""
    return user_id == requester_id or user_id in ALLOWED_USER_IDS


def progress_kb(job: Job) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text="❌ Отменить", callback_data=f"c:{job.id}")]
    if job.kind == "video":
        buttons.insert(0, InlineKeyboardButton(text="🎵 Только звук", callback_data=f"a:{job.id}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def cache_key(key: str, kind: str) -> str:
    return key if kind == "video" else f"{key}#{kind}"


def cache_get(key: str, kind: str) -> dict | None:
    entry = file_ids.get(cache_key(key, kind))
    if isinstance(entry, str):  # записи, сделанные до появления названий
        return {"file_id": entry, "title": ""}
    return entry


def cache_put(key: str, kind: str, file_id: str, title: str) -> None:
    file_ids[cache_key(key, kind)] = {"file_id": file_id, "title": title}
    save_cache()
rate_limited_until: dict[str, float] = {}  # "ig"/"yt" → до какого времени не трогать

SERVICE_NAMES = {"ig": "Instagram", "yt": "YouTube", "x": "X", "tt": "TikTok"}


def service_of(key: str) -> str:
    return key.split(":", 1)[0]


def cooldown_left(service: str) -> float:
    """Сколько секунд ещё нельзя обращаться к сервису после 429."""
    return max(0.0, rate_limited_until.get(service, 0.0) - time.monotonic())


def load_cache() -> None:
    try:
        file_ids.update(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
        log.info("кеш отправленных видео: %d записей", len(file_ids))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("не смог прочитать кеш %s: %s", CACHE_FILE, exc)


def save_cache() -> None:
    try:
        CACHE_FILE.write_text(json.dumps(file_ids, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.warning("не смог сохранить кеш: %s", exc)


def url_key(url: str) -> str:
    """Ключ видео без мусорных параметров: ?list=…, ?si=…, &t=… не создают дублей."""
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    if host == "youtu.be" and path:
        return f"yt:{path[0]}"
    if host.endswith("youtube.com"):
        video_id = parse_qs(parts.query).get("v")
        if video_id:
            return f"yt:{video_id[0]}"
        if len(path) >= 2 and path[0] in ("shorts", "live", "embed"):
            return f"yt:{path[1]}"
    if "instagram" in host and len(path) >= 2:
        # весь путь целиком: у историй он вида /stories/автор/id, и по первым двум
        # сегментам разные истории одного автора слились бы в один ключ
        return "ig:" + ":".join(path)
    if host.endswith("tiktok.com") and path:
        # /@user/video/123456 — длинная ссылка; vm./vt./t/КОД — короткая, ключуем по коду
        if "video" in path and len(path) > path.index("video") + 1:
            return f"tt:{path[path.index('video') + 1]}"
        return f"tt:{path[-1]}"
    if host.endswith(("x.com", "twitter.com")) and "status" in path:
        # /username/status/123456 — имя автора в ключ не берём, важен только id поста
        status_at = path.index("status")
        if len(path) > status_at + 1:
            return f"x:{path[status_at + 1]}"
    # ссылка незнакомого вида: берём и путь, и параметры. Без параметров, например,
    # два разных плейлиста YouTube дали бы один ключ и кеш вернул бы чужое видео
    tail = f"?{parts.query}" if parts.query else ""
    return f"{host}{parts.path}".rstrip("/") + tail

WELCOME = (
    "Привет! Пришли мне ссылку на видео из YouTube (в т.ч. Shorts), "
    "Instagram (Reels, посты), TikTok или X — я скачаю его и отправлю сюда.\n\n"
    f"Короткие ролики качаю в {MAX_HEIGHT}p, длинные и тяжёлые (от {HD_MAX_SIZE_MB} МБ "
    f"или {HD_MAX_DURATION_MIN} мин) — в {PREFERRED_HEIGHT}p. "
    f"Максимальный размер файла — {MAX_FILE_SIZE_MB} МБ."
)


class TooLargeError(Exception):
    """Видео не влезает в лимит Telegram даже в минимальном качестве."""


def is_allowed(message: Message) -> bool:
    if not ALLOWED_USER_IDS and not ALLOWED_CHAT_IDS:
        return True
    if message.from_user and message.from_user.id in ALLOWED_USER_IDS:
        return True
    return message.chat.id in ALLOWED_CHAT_IDS


def one_line(text: str | None, limit: int = 64) -> str:
    """Имена и названия чатов задаёт посторонний: переносы строк из них
    позволили бы дописывать в лог поддельные строки."""
    return " ".join((text or "").split())[:limit]


def describe_sender(message: Message) -> str:
    """Кто и откуда попросил — для логов."""
    user = message.from_user
    if not user:
        who = "неизвестный"
    elif user.username:
        who = f"@{one_line(user.username)} (id={user.id})"
    else:
        who = f"{one_line(user.full_name)} (id={user.id})"
    if message.chat.type == ChatType.PRIVATE:
        return f"{who} в личке"
    return f"{who} в группе «{one_line(message.chat.title)}» ({message.chat.id})"


def is_supported(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in SUPPORTED_HOSTS)


def use_cookies_for(service: str) -> bool:
    return bool(COOKIES_FILE) and ("all" in COOKIES_SERVICES or service in COOKIES_SERVICES)


def _base_opts(service: str = "") -> dict:
    opts = {
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        # при 429 каждый повтор только продлевает блокировку, поэтому их поменьше
        "extractor_retries": 1,
    }
    if use_cookies_for(service):
        opts["cookiefile"] = COOKIES_FILE
    if PROXY:
        opts["proxy"] = PROXY
    return opts


def _audio_opts(workdir: Path, on_progress=None, service: str = "") -> dict:
    hooks = {"progress_hooks": [on_progress]} if on_progress else {}
    return _base_opts(service) | hooks | {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "max_filesize": SIZE_TARGET,
        "restrictfilenames": True,
        "noprogress": True,
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},  # исполнитель и название прямо в файл
            {"key": "EmbedThumbnail"},  # обложкой станет превью ролика
        ],
    }


def _ydl_opts(workdir: Path, height: int, on_progress=None, service: str = "") -> dict:
    # H.264 в приоритете: YouTube отдаёт AV1 примерно в 10 раз медленнее,
    # да и играется H.264 на любом клиенте Telegram
    fmt = (
        f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height<={height}][vcodec^=avc1]+ba/"
        f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={height}]+ba/"
        f"b[height<={height}]/b"
    )
    hooks = {"progress_hooks": [on_progress]} if on_progress else {}
    return _base_opts(service) | hooks | {
        "format": fmt,
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "max_filesize": SIZE_TARGET,
        # YouTube душит соединение тем сильнее, чем дольше оно живёт: без этого
        # скорость по ходу скачивания сползает с ~10 МБ/с до сотен КБ/с
        "throttledratelimit": THROTTLE_FLOOR_KB * 1024,
        "restrictfilenames": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 4,
    }


def _pick_entry(info: dict) -> dict:
    # у каруселей/плейлистов берём первый элемент
    while info.get("entries") is not None:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise yt_dlp.utils.DownloadError("по ссылке не нашлось ни одного видео")
        info = entries[0]
    return info


def _result_file(info: dict) -> Path | None:
    for req in info.get("requested_downloads") or []:
        path = req.get("filepath")
        if path and Path(path).exists():
            return Path(path)
    return None


def _estimate_size(info: dict, height: int) -> int | None:
    """Грубая оценка размера видео на высоте height в байтах (None, если данных нет)."""
    duration = info.get("duration")
    candidates = [
        f for f in info.get("formats") or []
        if f.get("vcodec") not in (None, "none") and 0 < (f.get("height") or 0) <= height
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))
    size = best.get("filesize") or best.get("filesize_approx")
    if not size and best.get("tbr") and duration:
        size = best["tbr"] * 1000 / 8 * duration
    if not size:
        return None
    if best.get("acodec") in (None, "none") and duration:
        size += 128_000 / 8 * duration  # к видеодорожке добавляем аудио ~128 кбит/с
    return int(size)


def choose_start_height(info: dict) -> int:
    """С какого качества начинать: 1080p только для коротких и лёгких видео."""
    if PREFERRED_HEIGHT >= MAX_HEIGHT:
        return MAX_HEIGHT
    duration = info.get("duration") or 0
    if duration > HD_MAX_DURATION_MIN * 60:
        log.info("видео длиннее %d мин → качаю в %dp", HD_MAX_DURATION_MIN, PREFERRED_HEIGHT)
        return PREFERRED_HEIGHT
    estimate = _estimate_size(info, MAX_HEIGHT)
    if estimate and estimate > HD_MAX_SIZE_MB * 1024 * 1024:
        log.info(
            "в %dp это ~%d МБ (> %d МБ) → качаю в %dp",
            MAX_HEIGHT, estimate // 1024 // 1024, HD_MAX_SIZE_MB, PREFERRED_HEIGHT,
        )
        return PREFERRED_HEIGHT
    return MAX_HEIGHT


def fmt_size(num_bytes: float) -> str:
    mb = num_bytes / 1024 / 1024
    if mb >= 1024:
        return f"{mb / 1024:.2f} ГБ"
    return f"{mb:.0f} МБ" if mb >= 10 else f"{mb:.1f} МБ"


def fmt_speed(bytes_per_sec: float) -> str:
    kb = bytes_per_sec / 1024
    return f"{kb / 1024:.1f} МБ/с" if kb >= 1024 else f"{kb:.0f} КБ/с"


def fmt_time(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}" if minutes else f"{secs} сек"


class ProgressReporter:
    """Обновляет статусное сообщение по ходу скачивания.

    Хуки yt-dlp приходят из рабочего потока, поэтому правку сообщения
    отправляем в event loop через run_coroutine_threadsafe.
    """

    def __init__(
        self, loop: asyncio.AbstractEventLoop, status: Message, job: "Job | None" = None
    ) -> None:
        self._loop = loop
        self._status = status
        self._job = job
        self._last_at = 0.0
        self._last_text = ""

    def hook(self, data: dict) -> None:
        if self._job:
            self._job.check()  # нажали «Отменить» — прерываем скачивание
        status = data.get("status")
        if status == "finished":
            self._show("⏳ Обрабатываю видео…")
            return
        if status != "downloading":
            return
        if time.monotonic() - self._last_at < PROGRESS_INTERVAL:
            return
        self._last_at = time.monotonic()

        done = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        line = f"⬇️ Скачиваю: {fmt_size(done)}"
        if total:
            line += f" из {fmt_size(total)} ({done * 100 // total}%)"
        # средняя скорость вместо мгновенной: у фрагментных загрузок она скачет
        # от сотен КБ/с до десятков МБ/с и пугает выдуманным временем ожидания
        elapsed = data.get("elapsed") or 0
        if elapsed > 1 and done:
            average = done / elapsed
            extra = [fmt_speed(average)]
            if total and total > done:
                extra.append(f"осталось ~{fmt_time((total - done) / average)}")
            line += "\n" + " · ".join(extra)
        self._show(line)

    def _show(self, text: str) -> None:
        if text == self._last_text:
            return
        self._last_text = text
        asyncio.run_coroutine_threadsafe(self._edit(text), self._loop)

    async def _edit(self, text: str) -> None:
        try:
            # клавиатуру передаём каждый раз: правка без неё убрала бы кнопки
            markup = progress_kb(self._job) if self._job and not self._job.cancelled else None
            await self._status.edit_text(text, reply_markup=markup)
        except Exception as exc:  # статус не должен ломать скачивание
            log.debug("не смог обновить статус: %s", exc)


def download_audio(url: str, workdir: Path, on_progress=None) -> tuple[Path, dict]:
    """Забирает только звуковую дорожку и кладёт её в mp3 с тегами и обложкой."""
    workdir.mkdir(parents=True, exist_ok=True)
    service = service_of(url_key(url))
    with yt_dlp.YoutubeDL(_audio_opts(workdir, on_progress, service)) as ydl:
        info = _pick_entry(ydl.extract_info(url, download=True))
    audio = next((f for f in workdir.glob("*.mp3")), None)
    if audio is None:  # постпроцессор не отработал — отдаём что скачалось
        audio = _result_file(info)
    if audio is None or not audio.exists():
        raise yt_dlp.utils.DownloadError("не удалось выделить звуковую дорожку")
    if audio.stat().st_size > MAX_FILE_SIZE:
        raise TooLargeError
    return audio, info


def download_video(url: str, workdir: Path, on_progress=None) -> tuple[Path, dict]:
    """Скачивает видео, подбирая качество так, чтобы файл влез в лимит Telegram."""
    service = service_of(url_key(url))
    with yt_dlp.YoutubeDL(_base_opts(service)) as ydl:
        probe = _pick_entry(ydl.extract_info(url, download=False))
    start_height = choose_start_height(probe)
    ladder = tuple(h for h in HEIGHT_LADDER if h <= start_height) or (HEIGHT_LADDER[-1],)
    for height in ladder:
        attempt_dir = workdir / f"h{height}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(attempt_dir, height, on_progress, service)) as ydl:
                info = _pick_entry(ydl.extract_info(url, download=True))
        except yt_dlp.utils.DownloadError as exc:
            if "max-filesize" in str(exc).lower():
                continue
            raise
        path = _result_file(info)
        if path is None:
            # yt-dlp прервал скачивание (файл превысил лимит) — пробуем качество ниже
            continue
        if path.stat().st_size <= MAX_FILE_SIZE:
            return path, info
        path.unlink(missing_ok=True)
    raise TooLargeError


TRANSIENT_ERROR_MARKERS = (
    "403", "forbidden", "timed out", "timeout", "connection reset", "temporary", "http error 5",
)


def is_transient_dlp_error(exc: yt_dlp.utils.DownloadError) -> bool:
    low = ANSI_RE.sub("", str(exc)).lower()
    return any(marker in low for marker in TRANSIENT_ERROR_MARKERS)


def is_rate_limited(exc: yt_dlp.utils.DownloadError) -> bool:
    low = ANSI_RE.sub("", str(exc)).lower()
    return "429" in low or "too many requests" in low


def friendly_dlp_error(exc: yt_dlp.utils.DownloadError, service: str = "") -> str:
    text = ANSI_RE.sub("", str(exc)).removeprefix("ERROR: ").strip()
    low = text.lower()
    if is_rate_limited(exc):
        name = SERVICE_NAMES.get(service, "Сервис")
        return (
            f"🚦 {name} временно ограничил нас: сегодня из него скачали много всего, "
            f"и он просит передохнуть.\n"
            f"Подожду {RATE_LIMIT_COOLDOWN_MIN} минут и снова буду принимать ссылки — "
            f"повторять сейчас бесполезно, от этого блокировка только продлевается."
        )
    if "no video formats found" in low:
        return "🖼 В этом посте нет видео — только фото. Скачивать нечего."
    if "400" in low and "bad request" in low:
        return (
            "🔑 Instagram разлогинил бота: сессия в cookies больше не действует.\n"
            "Нужно заново выгрузить cookies.txt из браузера — до этого рилсы качаться не будут."
        )
    if "empty media response" in low:
        return (
            "🔒 Instagram не отдал это видео анонимно — скорее всего, пост "
            "из закрытого или возрастного аккаунта. Нужны cookies (см. README)."
        )
    if "403" in low and "forbidden" in low:
        return (
            f"🚧 Сервер {DOWNLOAD_ATTEMPTS} раза подряд отклонил скачивание (HTTP 403) — "
            "похоже, наш IP временно придерживают. Подожди минут десять и пришли ссылку ещё раз."
        )
    if "confirm your age" in low or "age-restricted" in low:
        return (
            "🔞 У этого видео возрастное ограничение — YouTube отдаёт его только "
            "залогиненным, а cookies YouTube у бота нет. Скачать не получится."
        )
    if "404" in low and "not found" in low:
        return "🗑 Похоже, пост удалён или ссылка битая (404)."
    if "sign in to confirm" in low or "not a bot" in low:
        return (
            "🤖 YouTube принял меня за бота (что справедливо) и требует вход в аккаунт.\n"
            "Добавь cookies: переменная COOKIES_FILE в .env, инструкция в README."
        )
    if "login required" in low or "rate-limit" in low or "requested content is not available" in low:
        return (
            "🔒 Instagram отдаёт это видео только залогиненным.\n"
            "Добавь cookies: переменная COOKIES_FILE в .env, инструкция в README."
        )
    if "private" in low:
        return "🔒 Это приватное видео — без cookies аккаунта, у которого есть доступ, не скачать."
    if "unsupported url" in low:
        return "🤷 Не смог распознать эту ссылку. Проверь, что она ведёт на конкретное видео."
    return f"💥 Не получилось скачать:\n<code>{html.escape(text[:300])}</code>"


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    if not is_allowed(message):
        if message.chat.type == ChatType.PRIVATE:
            await message.reply("⛔ Это личный бот, доступ только по списку.")
        return
    await message.answer(WELCOME)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    # владельцу отвечаем в любом чате (его id в списке), посторонним — молчим
    if not is_allowed(message):
        return
    await message.reply(
        f"id этого чата: <code>{message.chat.id}</code>\n"
        f"твой user id: <code>{message.from_user.id if message.from_user else '—'}</code>"
    )


@router.message(F.text)
async def handle_link(message: Message) -> None:
    is_private = message.chat.type == ChatType.PRIVATE
    allowed = is_allowed(message)
    if is_private and not allowed:
        await message.reply("⛔ Это личный бот, доступ только по списку.")
        return

    match = URL_RE.search(message.text)
    if not match:
        # в группах на обычную болтовню не реагируем
        if is_private:
            await message.reply("Пришли ссылку на видео из YouTube, Instagram, TikTok или X.")
        return
    url = match.group(0)
    if not ALLOW_ANY_SITE and not is_supported(url):
        if is_private:
            await message.reply(
                "Я скачиваю только из YouTube, Instagram, TikTok и X.\n"
                "Хочешь другие сайты — включи ALLOW_ANY_SITE=true в .env."
            )
        return
    if not allowed:
        await message.reply(
            "⛔ Эта группа не в списке разрешённых.\n"
            "Пришли /id и добавь id чата в ALLOWED_CHAT_IDS в .env бота."
        )
        return

    key = url_key(url)
    who = describe_sender(message)
    log.info("запрос: %s → %s [%s]", who, url, key)
    try:
        status = await message.reply("🔍 Смотрю, что за видео…")
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        # бота ограничили в чате — молча пропускаем, иначе каждая ссылка сыпет трейсбеки
        log.warning("не могу писать в чате %s (%s) — пропускаю ссылку", message.chat.id, exc.message)
        return
    lock = url_locks.setdefault(key, asyncio.Lock())
    if lock.locked() and key not in file_ids:
        log.info("[%s] уже качается, %s ждёт результат", key, who)
        await status.edit_text("⏳ Это видео уже качается — дождусь и пришлю сюда тоже.")
    async with lock:
        await deliver(message, status, url, key, who, "video")


async def show_error(status: Message, text: str) -> None:
    """Показывает ошибку; в группах сообщение самоудаляется, чтобы не засорять чат."""
    try:
        await status.edit_text(text)
    except Exception as exc:
        log.debug("не смог показать ошибку: %s", exc)
        return
    if ERROR_TTL_SEC and status.chat.type != ChatType.PRIVATE:
        async def _cleanup() -> None:
            await asyncio.sleep(ERROR_TTL_SEC)
            try:
                await status.delete()
            except Exception:
                pass
        asyncio.create_task(_cleanup())


async def deliver(
    message: Message, status: Message, url: str, key: str, who: str, kind: str
) -> None:
    """Отдаёт видео или звук: из кеша, если уже качали, иначе скачивает."""
    if await send_cached(message, status, key, kind):
        log.info("[%s/%s] отправлено из кеша для %s", key, kind, who)
        return
    service = service_of(key)
    left = cooldown_left(service)
    if left:
        log.info("[%s] в паузе после 429, осталось %s", key, fmt_time(left))
        await show_error(
            status,
            f"🚦 {SERVICE_NAMES.get(service, 'Сервис')} ограничил нас по частоте запросов. "
            f"Подожди ещё {fmt_time(left)} и пришли ссылку заново.",
        )
        return
    await download_and_send(message, status, url, key, who, kind)


async def send_cached(message: Message, status: Message, key: str, kind: str) -> bool:
    """Уже отправляли — Telegram перешлёт файл по file_id мгновенно."""
    entry = cache_get(key, kind)
    if not entry:
        return False
    caption = entry.get("title") or None
    try:
        if kind == "audio":
            await message.reply_audio(entry["file_id"], caption=caption)
        else:
            await message.reply_video(
                entry["file_id"], caption=caption, supports_streaming=True
            )
        await status.delete()
        return True
    except Exception as exc:
        log.warning("не смог отправить из кеша (%s), качаю заново: %s", key, exc)
        file_ids.pop(cache_key(key, kind), None)
        save_cache()
        return False


async def download_and_send(
    message: Message, status: Message, url: str, key: str, who: str, kind: str
) -> None:
    requester_id = message.from_user.id if message.from_user else 0
    job = Job(requester_id, kind)
    reporter = ProgressReporter(asyncio.get_running_loop(), status, job)
    grab = download_audio if kind == "audio" else download_video
    workdir = DOWNLOAD_DIR / uuid.uuid4().hex
    started = time.monotonic()
    try:
        await status.edit_text(
            "🔍 Смотрю, что за видео…" if kind == "video" else "🎵 Достаю звуковую дорожку…",
            reply_markup=progress_kb(job),
        )
        # временные ошибки (403 и т.п.) пересиливаем сами: пауза и новая попытка
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                async with download_slots:
                    job.check()
                    path, info = await asyncio.wait_for(
                        asyncio.to_thread(grab, url, workdir, reporter.hook),
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                break
            except yt_dlp.utils.DownloadError as exc:
                if attempt == DOWNLOAD_ATTEMPTS or not is_transient_dlp_error(exc):
                    raise
                delay = RETRY_DELAYS[attempt - 1]
                log.warning(
                    "попытка %d/%d не удалась (%s), повтор через %d c: %s",
                    attempt, DOWNLOAD_ATTEMPTS, url, delay, exc,
                )
                await status.edit_text(
                    f"🚧 Сервер отбил скачивание, повторю через {delay} сек… "
                    f"(попытка {attempt + 1} из {DOWNLOAD_ATTEMPTS})"
                )
                await asyncio.sleep(delay)
        job.check()
        size = path.stat().st_size
        title = (info.get("title") or ("Аудио" if kind == "audio" else "Видео"))[:900]
        await status.edit_text(f"📤 Отправляю в Telegram… ({fmt_size(size)})")
        await message.bot.send_chat_action(
            message.chat.id,
            ChatAction.UPLOAD_VOICE if kind == "audio" else ChatAction.UPLOAD_VIDEO,
        )
        # шорткаты reply_* только собирают метод; отправляем его сами, чтобы задать
        # request_timeout — иначе аплоад больших файлов рвётся на 60 c
        if kind == "audio":
            send = message.reply_audio(
                FSInputFile(path),
                caption=html.escape(title),
                title=(info.get("track") or info.get("title") or "")[:64] or None,
                performer=(info.get("artist") or info.get("uploader") or "")[:64] or None,
                duration=int(info.get("duration") or 0) or None,
            )
        else:
            send = message.reply_video(
                FSInputFile(path),
                caption=html.escape(title),
                duration=int(info.get("duration") or 0) or None,
                width=info.get("width"),
                height=info.get("height"),
                supports_streaming=True,
            )
        sent = await message.bot(send, request_timeout=UPLOAD_TIMEOUT)
        media = sent.audio if kind == "audio" else sent.video
        if media:  # запомним, чтобы второй раз не качать
            cache_put(key, kind, media.file_id, title)
        log.info(
            "[%s/%s] готово за %s: %s, %s, для %s",
            key, kind, fmt_time(time.monotonic() - started), fmt_size(size),
            f"{info.get('height')}p" if kind == "video" else "mp3", who,
        )
        await status.delete()
    except CancelledByUser:
        if kind == "video" and job.kind == "audio":  # нажали «Только звук»
            log.info("[%s] переключаюсь на звук для %s", key, who)
            job.close()
            shutil.rmtree(workdir, ignore_errors=True)
            await deliver(message, status, url, key, who, "audio")
            return
        log.info("[%s/%s] отменено пользователем", key, kind)
        await show_error(status, "🚫 Закачка отменена.")
    except TooLargeError:
        hint = (
            "Больше — только ссылкой на файл: 2 ГБ это потолок самого Telegram."
            if MAX_FILE_SIZE_MB >= 2000
            else "Для файлов до 2 ГБ нужен локальный Bot API server — см. README."
        )
        limit_note = (
            f"😞 Звук не влезает в {MAX_FILE_SIZE_MB} МБ." if kind == "audio"
            else f"😞 Видео не влезает в {MAX_FILE_SIZE_MB} МБ даже в {HEIGHT_LADDER[-1]}p."
        )
        await show_error(status, f"{limit_note}\n{hint}")
    except asyncio.TimeoutError:
        await show_error(status, "⌛ Скачивание не уложилось в таймаут. Попробуй ещё раз или видео покороче.")
    except yt_dlp.utils.DownloadError as exc:
        service = service_of(key)
        if is_rate_limited(exc):
            rate_limited_until[service] = time.monotonic() + RATE_LIMIT_COOLDOWN_MIN * 60
            log.warning(
                "[%s] поймали 429 — не трогаем %s ближайшие %d мин",
                key, SERVICE_NAMES.get(service, service), RATE_LIMIT_COOLDOWN_MIN,
            )
        else:
            log.warning("yt-dlp error for %s: %s", url, exc)
        await show_error(status, friendly_dlp_error(exc, service))
    except TelegramNetworkError as exc:
        log.warning("upload failed for %s: %s", url, exc)
        await show_error(
            status,
            "📶 Скачал, но не смог загрузить файл в Telegram — оборвалась сеть или "
            "не хватило таймаута на отправку. Попробуй ещё раз; если повторяется, "
            "увеличь UPLOAD_TIMEOUT в .env.",
        )
    except Exception:
        log.exception("Не смог обработать %s", url)
        await show_error(status, "💥 Что-то пошло не так. Подробности в логах бота.")
    finally:
        job.close()
        shutil.rmtree(workdir, ignore_errors=True)


@router.callback_query(F.data.startswith("c:"))
async def cb_cancel(query: CallbackQuery) -> None:
    job = jobs.get(query.data.split(":", 1)[1])
    if not job:
        await query.answer("Эта закачка уже завершилась.", show_alert=True)
        return
    if not may_manage(query.from_user.id, job.requester_id):
        await query.answer("Отменить может только тот, кто прислал ссылку.", show_alert=True)
        return
    job.cancelled = True
    await query.answer("Отменяю…")


@router.callback_query(F.data.startswith("a:"))
async def cb_audio(query: CallbackQuery) -> None:
    """«Только звук»: бросаем видео и качаем ту же ссылку аудиодорожкой."""
    job = jobs.get(query.data.split(":", 1)[1])
    if not job:
        await query.answer("Эта закачка уже завершилась.", show_alert=True)
        return
    if not may_manage(query.from_user.id, job.requester_id):
        await query.answer("Переключить может только тот, кто прислал ссылку.", show_alert=True)
        return
    job.cancelled = True
    job.kind = "audio"  # download_and_send перезапустит закачку в аудиорежиме
    await query.answer("Переключаюсь на звук…")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан: скопируй .env.example в .env и впиши токен от @BotFather.")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    load_cache()
    session = None
    if TELEGRAM_API_URL:
        # локальный Bot API server: лимит на отправку 2 ГБ вместо 50 МБ
        session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API_URL, is_local=True))
    bot = Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info(
        "Бот @%s запущен: API=%s, лимит файла %d МБ, качество до %dp",
        me.username, TELEGRAM_API_URL or "api.telegram.org", MAX_FILE_SIZE_MB, MAX_HEIGHT,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
