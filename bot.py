"""AlexShepDownloadBot — Telegram-бот для скачивания видео из YouTube, Instagram, TikTok и X.

Запуск: python bot.py (настройки берутся из .env, см. .env.example).
"""

import asyncio
import hashlib
import html
import itertools
import json
import logging
import os
import re
import shutil
import struct
import subprocess
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
    InputMediaPhoto,
    InputMediaVideo,
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
# Этим сервисам cookies подставляются только со второй попытки — когда первая,
# анонимная, упёрлась в «нужен вход» (бот-проверка, возрастное ограничение).
# Так аккаунт светится лишь там, где без него никак.
COOKIES_FALLBACK_SERVICES = {
    s for s in os.getenv("COOKIES_FALLBACK_SERVICES", "yt").replace(" ", "").lower().split(",") if s
}
PROXY = os.getenv("PROXY", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR") or Path(tempfile.gettempdir()) / "alexshep_download_bot")
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "900"))
UPLOAD_TIMEOUT = int(os.getenv("UPLOAD_TIMEOUT", "600"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
# Сколько запросов может одновременно выполняться или ждать своей очереди.
# Семафор ниже ограничивает только сами скачивания, поэтому без отдельного потолка
# один пользователь мог бы создать неограниченное число ожидающих asyncio-задач.
MAX_PENDING_REQUESTS = int(os.getenv("MAX_PENDING_REQUESTS", "12"))
MAX_REQUESTS_PER_USER = int(os.getenv("MAX_REQUESTS_PER_USER", "3"))
MAX_CACHE_ENTRIES = int(os.getenv("MAX_CACHE_ENTRIES", "5000"))
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
# Telegram на iPhone не играет VP9/AV1 и 10-битный H.264: такое перекодируем в H.264,
# но только ролики не длиннее этого: 2 ядра кодируют 1080p чуть медленнее реального времени
TRANSCODE_MAX_DURATION_MIN = int(os.getenv("TRANSCODE_MAX_DURATION_MIN", "5"))
# как часто проверять, что сессия Instagram в cookies ещё жива, минут
IG_SESSION_CHECK_MIN = int(os.getenv("IG_SESSION_CHECK_MIN", "60"))

DOWNLOAD_ATTEMPTS = 3  # сколько раз пробовать при временных ошибках (403 и т.п.)
RETRY_DELAYS = (15, 45)  # паузы перед 2-й и 3-й попыткой, сек

MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
SIZE_TARGET = int(MAX_FILE_SIZE * 0.96)  # целимся с запасом под лимит
MAX_METADATA_SIZE = 5 * 1024 * 1024
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
request_state_lock = asyncio.Lock()
pending_requests = 0
pending_by_user: dict[int, int] = {}

# одно и то же видео качаем один раз: второй чат ждёт на замке и получает готовый file_id
CACHE_FILE = Path(os.getenv("CACHE_FILE") or "file_ids.json")
file_ids: dict[str, dict] = {}  # ключ → {"file_id": …, "title": …}
class UrlLock:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


url_locks: dict[str, UrlLock] = {}
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


def trim_cache() -> bool:
    trimmed = False
    while len(file_ids) > MAX_CACHE_ENTRIES:
        file_ids.pop(next(iter(file_ids)))
        trimmed = True
    return trimmed


def cache_put(key: str, kind: str, file_id: str, title: str) -> None:
    full_key = cache_key(key, kind)
    file_ids.pop(full_key, None)  # повторная запись становится самой свежей
    file_ids[full_key] = {"file_id": file_id, "title": title}
    trim_cache()
    save_cache()


def cache_put_album(key: str, kind: str, album: list[list[str]], title: str) -> None:
    """Альбом фото поста: [["photo", file_id], ["video", file_id], …]."""
    full_key = cache_key(key, kind)
    file_ids.pop(full_key, None)
    file_ids[full_key] = {"album": album, "title": title}
    trim_cache()
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
        loaded = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("корень кеша должен быть объектом")
        file_ids.update(loaded)
        if trim_cache():
            save_cache()
        log.info("кеш отправленных видео: %d записей", len(file_ids))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("не смог прочитать кеш %s: %s", CACHE_FILE, safe_error(exc))


def save_cache() -> None:
    temp = CACHE_FILE.with_name(f".{CACHE_FILE.name}.tmp")
    try:
        temp.write_text(json.dumps(file_ids, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, CACHE_FILE)
    except Exception as exc:
        temp.unlink(missing_ok=True)
        log.warning("не смог сохранить кеш: %s", safe_error(exc))


def safe_key_part(value: str) -> str:
    """Оставляет читаемые media id, но не допускает управляющие символы в кеш и лог."""
    if re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"hash-{digest}"


def url_key(url: str) -> str:
    """Ключ видео без мусорных параметров: ?list=…, ?si=…, &t=… не создают дублей."""
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    if host == "youtu.be" and path:
        return f"yt:{safe_key_part(path[0])}"
    if host.endswith("youtube.com"):
        video_id = parse_qs(parts.query).get("v")
        if video_id:
            return f"yt:{safe_key_part(video_id[0])}"
        if len(path) >= 2 and path[0] in ("shorts", "live", "embed"):
            return f"yt:{safe_key_part(path[1])}"
    if "instagram" in host and path:
        # весь путь целиком: у историй он вида /stories/автор/id, и по первым двум
        # сегментам разные истории одного автора слились бы в один ключ
        return "ig:" + ":".join(safe_key_part(part) for part in path)
    if host.endswith("tiktok.com") and path:
        # /@user/video/123456 — длинная ссылка; vm./vt./t/КОД — короткая, ключуем по коду
        if "video" in path and len(path) > path.index("video") + 1:
            return f"tt:{safe_key_part(path[path.index('video') + 1])}"
        return f"tt:{safe_key_part(path[-1])}"
    if host.endswith(("x.com", "twitter.com")) and "status" in path:
        # /username/status/123456 — имя автора в ключ не берём, важен только id поста
        status_at = path.index("status")
        if len(path) > status_at + 1:
            return f"x:{safe_key_part(path[status_at + 1])}"
    # Для неизвестного пути сохраняем только хеш: query может содержать приватный
    # share-токен, а ключ попадает в кеш и логи.
    if host == "youtu.be" or host.endswith("youtube.com"):
        service = "yt"
    elif host.endswith(("instagram.com", "instagr.am")):
        service = "ig"
    elif host.endswith("tiktok.com"):
        service = "tt"
    elif host.endswith(("x.com", "twitter.com")):
        service = "x"
    else:
        service = "url"
    digest = hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"{service}:url:{digest}"

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
    if message.from_user and message.from_user.id in ALLOWED_USER_IDS:
        return True
    return message.chat.id in ALLOWED_CHAT_IDS


def requester_id_of(message: Message) -> int:
    """Стабильный ключ для лимита: пользователь, а для анонимного сообщения — чат."""
    if message.from_user:
        return message.from_user.id
    if message.sender_chat:
        return message.sender_chat.id
    return message.chat.id


async def reserve_request(requester_id: int) -> str | None:
    """Резервирует место в очереди; возвращает причину отказа или None."""
    global pending_requests
    async with request_state_lock:
        if pending_requests >= MAX_PENDING_REQUESTS:
            return "global"
        if pending_by_user.get(requester_id, 0) >= MAX_REQUESTS_PER_USER:
            return "user"
        pending_requests += 1
        pending_by_user[requester_id] = pending_by_user.get(requester_id, 0) + 1
    return None


async def release_request(requester_id: int) -> None:
    global pending_requests
    async with request_state_lock:
        pending_requests = max(0, pending_requests - 1)
        left = pending_by_user.get(requester_id, 0) - 1
        if left > 0:
            pending_by_user[requester_id] = left
        else:
            pending_by_user.pop(requester_id, None)


def retain_url_lock(key: str) -> UrlLock:
    entry = url_locks.get(key)
    if entry is None:
        entry = url_locks[key] = UrlLock()
    entry.refs += 1
    return entry


def release_url_lock(key: str, entry: UrlLock) -> None:
    entry.refs -= 1
    if entry.refs == 0 and url_locks.get(key) is entry:
        url_locks.pop(key, None)


def one_line(text: str | None, limit: int = 64) -> str:
    """Имена и названия чатов задаёт посторонний: переносы строк из них
    позволили бы дописывать в лог поддельные строки."""
    return " ".join((text or "").split())[:limit]


def safe_url_for_log(url: str, limit: int = 300) -> str:
    """URL без пароля, query и fragment: их содержимое нередко является секретом."""
    try:
        parts = urlparse(url)
        host = (parts.hostname or "").lower()
        if not host:
            return "<invalid-url>"
        value = f"{parts.scheme.lower()}://{host}{parts.path or '/'}"
    except (TypeError, ValueError):
        return "<invalid-url>"
    return one_line(value, limit)


def safe_error(exc: BaseException, limit: int = 300) -> str:
    """Убирает URL-токены и переносы строк из текста внешней ошибки перед логированием."""
    text = ANSI_RE.sub("", str(exc))
    text = URL_RE.sub(lambda match: safe_url_for_log(match.group(0)), text)
    return one_line(text, limit)


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
    try:
        parts = urlparse(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    return any(host == h or host.endswith("." + h) for h in SUPPORTED_HOSTS)


def use_cookies_for(service: str) -> bool:
    return bool(COOKIES_FILE) and ("all" in COOKIES_SERVICES or service in COOKIES_SERVICES)


def has_cookie_fallback(service: str) -> bool:
    return (
        bool(COOKIES_FILE)
        and service in COOKIES_FALLBACK_SERVICES
        and not use_cookies_for(service)
    )


def cookie_expiry(domain: str, name: str) -> float | None:
    """Когда истекает cookie из COOKIES_FILE (0 — до закрытия браузера); None — её нет."""
    try:
        lines = Path(COOKIES_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.removeprefix("#HttpOnly_")  # так Netscape-формат помечает httpOnly cookies
        fields = line.split("\t")
        if line.startswith("#") or len(fields) != 7:
            continue
        if fields[0].lstrip(".").endswith(domain) and fields[5] == name and fields[6]:
            return float(fields[4] or 0)
    return None


def ig_session_alive() -> bool:
    """Instagram разлогинивает, стирая sessionid: yt-dlp послушно удаляет её из файла."""
    expiry = cookie_expiry("instagram.com", "sessionid")
    return expiry is not None and (expiry == 0 or expiry > time.time())


# последнее известное состояние сессии Instagram: ok | missing | checkpoint.
# Храним, чтобы писать владельцу только о переменах.
ig_session_state: str | None = None


def ig_state_message(state: str) -> str:
    return {
        "ok": "✅ Сессия Instagram снова работает — рилсы качаются с аккаунтом.",
        "missing": (
            "🔑 Instagram разлогинил бота: в cookies больше нет сессии (sessionid).\n"
            "Пока рилсы качаются анонимно — жди 429 и «недоступно для некоторых аудиторий».\n"
            f"Выгрузи cookies instagram.com заново и положи на сервер в <code>{html.escape(COOKIES_FILE)}</code>."
        ),
        "checkpoint": (
            "🔐 Instagram заблокировал вход в аккаунт бота и ждёт подтверждения «Это были вы?» "
            "(checkpoint_required). Пока проверка не пройдена, рилсы с аккаунтом не качаются.\n"
            "Открой этот аккаунт в приложении Instagram и подтверди вход — cookies менять не нужно."
        ),
    }[state]


async def notify_owners(bot: Bot, text: str) -> None:
    for user_id in ALLOWED_USER_IDS:
        try:
            await bot.send_message(user_id, text)
        except Exception as exc:  # владелец не запускал бота в личке и т.п.
            log.warning("не смог написать владельцу %s: %s", user_id, safe_error(exc))


async def set_ig_state(bot: Bot, state: str) -> None:
    global ig_session_state
    if not use_cookies_for("ig") or state == ig_session_state:
        return
    previous, ig_session_state = ig_session_state, state
    log.log(logging.INFO if state == "ok" else logging.WARNING, "сессия Instagram: %s", state)
    if previous is None and state == "ok":  # при старте всё в порядке — молчим
        return
    await notify_owners(bot, ig_state_message(state))


def ig_checkpoint(url: str) -> bool:
    """Отказ из-за проверки входа? yt-dlp пишет только «400 Bad Request», причина — в теле ответа API."""
    pk = _ig_media_pk(url)
    if pk is None:
        return False
    with yt_dlp.YoutubeDL(_base_opts("ig")) as ydl:
        request = yt_dlp.networking.Request(
            f"https://i.instagram.com/api/v1/media/{pk}/info/", headers=IG_API_HEADERS
        )
        try:
            ydl.urlopen(request).read()
        except yt_dlp.networking.exceptions.HTTPError as exc:
            return "checkpoint_required" in exc.response.read(4000).decode(errors="replace")
        except Exception:
            return False
    return False


async def check_ig_session(bot: Bot, failed_url: str | None = None) -> None:
    """Проверяет сессию Instagram; failed_url — ссылка, на которой Instagram только что отказал."""
    if not use_cookies_for("ig"):
        return
    if not ig_session_alive():
        await set_ig_state(bot, "missing")
    elif failed_url and await asyncio.to_thread(ig_checkpoint, failed_url):
        await set_ig_state(bot, "checkpoint")
    elif ig_session_state in (None, "missing"):
        # cookies на месте; снятую проверку входа отсюда не увидеть — её подтвердит удачная закачка
        await set_ig_state(bot, "ok")


async def watch_ig_session(bot: Bot) -> None:
    while True:
        await check_ig_session(bot)
        await asyncio.sleep(IG_SESSION_CHECK_MIN * 60)


AUTH_ERROR_MARKERS = (
    "sign in", "not a bot", "confirm your age", "age-restricted",
    "login required", "members-only", "members only",
    # YouTube больше не отдаёт видео анонимно с серверных IP: без cookies yt-dlp
    # скатывается на клиент android_vr (у остальных нет PO-токена), и загрузка
    # обрывается на ~10 МБ. Формально это не «нужен вход», но лечится ровно так же.
    "403: forbidden",
)


def is_auth_error(exc: yt_dlp.utils.DownloadError) -> bool:
    low = ANSI_RE.sub("", str(exc)).lower()
    return any(marker in low for marker in AUTH_ERROR_MARKERS)


def _base_opts(service: str = "", with_cookies: bool = False) -> dict:
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
    if with_cookies or use_cookies_for(service):
        opts["cookiefile"] = COOKIES_FILE
    if PROXY:
        opts["proxy"] = PROXY
    return opts


def _audio_opts(
    workdir: Path, on_progress=None, service: str = "", with_cookies: bool = False
) -> dict:
    hooks = {"progress_hooks": [on_progress]} if on_progress else {}
    return _base_opts(service, with_cookies) | hooks | {
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


def _ydl_opts(
    workdir: Path,
    height: int,
    on_progress=None,
    service: str = "",
    with_cookies: bool = False,
    side: str = "height",
) -> dict:
    # H.264 в приоритете: YouTube отдаёт AV1 примерно в 10 раз медленнее,
    # а iPhone VP9/AV1 не играет вовсе. Качество ограничиваем по короткой стороне,
    # иначе вертикальный 1080×1920 не проходит фильтр «≤1080» и качается в 480p.
    limit = f"[{side}<={height}]"
    fmt = (
        f"bv*{limit}[vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*{limit}[vcodec^=avc1]+ba/"
        # готовый файл со звуком раньше раздельных дорожек неизвестного кодека:
        # у Instagram он H.264, хоть и не подписан, а DASH-дорожки там — VP9
        f"b{limit}/"
        f"bv*{limit}+ba/"
        "b"
    )
    hooks = {"progress_hooks": [on_progress]} if on_progress else {}
    return _base_opts(service, with_cookies) | hooks | {
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


def short_side(info: dict) -> str:
    """Какое измерение ограничивать качеством: у вертикального видео короткая сторона — ширина."""
    width, height = info.get("width") or 0, info.get("height") or 0
    if not (width and height):
        sized = [f for f in info.get("formats") or [] if f.get("width") and f.get("height")]
        if sized:
            biggest = max(sized, key=lambda f: f["width"] * f["height"])
            width, height = biggest["width"], biggest["height"]
    return "width" if 0 < width < height else "height"


def _estimate_size(info: dict, height: int, side: str = "height") -> int | None:
    """Грубая оценка размера видео в качестве height, байт (None, если данных нет)."""
    duration = info.get("duration")
    candidates = [
        f for f in info.get("formats") or []
        if f.get("vcodec") not in (None, "none") and 0 < (f.get(side) or 0) <= height
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda f: ((f.get(side) or 0), (f.get("tbr") or 0)))
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
    estimate = _estimate_size(info, MAX_HEIGHT, short_side(info))
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
        if status == "converting":
            self._show("🔄 Перекодирую видео, чтобы оно играло и на iPhone…")
            return
        if status == "slideshow":
            self._show("🎞 В посте фото с музыкой — собираю из них видео…")
            return
        if status == "photos":
            self._show("🖼 В посте только фото — забираю их…")
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


IOS_VIDEO_CODECS = {"h264"}
IOS_PIX_FMTS = {"yuv420p", "yuvj420p"}  # 10-битный H.264 (yuv420p10le) iPhone не декодирует
IOS_AUDIO_CODECS = {"aac", "mp3"}


def _moov_first(path: Path) -> bool:
    """Индекс mp4 (moov) лежит до данных (mdat)? Иначе видео не стримится, пока не скачано целиком."""
    with path.open("rb") as f:
        while header := f.read(8):
            if len(header) < 8:
                return False
            size, kind = struct.unpack(">I4s", header)
            if kind == b"moov":
                return True
            if kind == b"mdat":
                return False
            if size == 1:  # размер бокса не влез в 32 бита
                size = struct.unpack(">Q", f.read(8))[0] - 8
            elif size < 8:
                return False
            f.seek(size - 8, os.SEEK_CUR)
    return False


def _run_ffmpeg(args: list[str], on_progress, status: str = "converting") -> None:
    """ffmpeg в фоне; раз в секунду дёргаем хук, чтобы работала кнопка «Отменить»."""
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-v", "error", *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        while True:
            try:
                _, err = proc.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if on_progress:
                    on_progress({"status": status})
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if proc.returncode:
        tail = err.decode(errors="replace").strip()[-300:]
        raise yt_dlp.utils.DownloadError(f"ffmpeg не смог обработать видео: {tail}")


def make_ios_compatible(path: Path, info: dict, on_progress=None) -> Path:
    """Приводит видео к тому, что Telegram играет везде: H.264 8 бит + AAC, moov в начале.

    Заодно берёт размеры и длительность из самого файла: yt-dlp их знает не всегда,
    а без них Telegram показывает ролик квадратом или без превью.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    media = json.loads(probe.stdout or "{}")
    streams = media.get("streams") or []
    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if video is None:
        return path
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info["width"], info["height"] = video.get("width"), video.get("height")
    duration = float((media.get("format") or {}).get("duration") or 0)
    if duration:
        info["duration"] = duration

    video_ok = video.get("codec_name") in IOS_VIDEO_CODECS and video.get("pix_fmt") in IOS_PIX_FMTS
    audio_ok = audio is None or audio.get("codec_name") in IOS_AUDIO_CODECS
    if video_ok and audio_ok and _moov_first(path):
        return path
    if not video_ok and duration > TRANSCODE_MAX_DURATION_MIN * 60:
        log.info(
            "%s в %s/%s, но длиннее %d мин — отправляю как есть",
            path.name, video.get("codec_name"), video.get("pix_fmt"), TRANSCODE_MAX_DURATION_MIN,
        )
        return path

    if video_ok:
        video_args = ["-c:v", "copy"]
    else:
        video_args = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            # у yuv420p обе стороны должны быть чётными
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        ]
    audio_args = ["-c:a", "copy"] if audio_ok else ["-c:a", "aac", "-b:a", "160k"]
    log.info(
        "%s: видео %s/%s → %s, звук %s → %s",
        path.name, video.get("codec_name"), video.get("pix_fmt"), "copy" if video_ok else "h264",
        audio.get("codec_name") if audio else "—", "copy" if audio_ok else "aac",
    )
    out = path.with_name(f"{path.stem}.tg.mp4")
    _run_ffmpeg(
        ["-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", *video_args, *audio_args,
         "-movflags", "+faststart", str(out)],
        on_progress,
    )
    path.unlink(missing_ok=True)
    return out


IG_API_HEADERS = {
    "X-IG-App-ID": "936619743392459",  # id веб-клиента Instagram, тот же шлёт и сам yt-dlp
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0 Safari/537.36"
    ),
}
IG_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
NO_VIDEO_MARKERS = ("no video formats found", "there is no video in this post")
SLIDE_SEC = 3  # сколько показывать фото в карусели
SLIDE_MAX_SEC = 6  # фото мало, а музыки много — растягиваем, но не до бесконечности
SLIDESHOW_FPS = 25
SLIDESHOW_MAX_SEC = 180
SLIDESHOW_SIDE = 1080  # ширина кадра слайд-шоу


def is_no_video_error(exc: yt_dlp.utils.DownloadError) -> bool:
    low = ANSI_RE.sub("", str(exc)).lower()
    return any(marker in low for marker in NO_VIDEO_MARKERS)


def _ig_media_pk(url: str) -> int | None:
    """id поста для API: shortcode из ссылки — это число в base64 (у приватных после 11 символов хвост)."""
    path = [p for p in urlparse(url).path.split("/") if p]
    for marker in ("p", "reel", "reels", "tv"):
        if marker in path and len(path) > path.index(marker) + 1:
            pk = 0
            for char in path[path.index(marker) + 1][:11]:
                if char not in IG_SHORTCODE_ALPHABET:
                    return None
                pk = pk * 64 + IG_SHORTCODE_ALPHABET.index(char)
            return pk
    return None


def _copy_limited(source, target, max_bytes: int) -> None:
    """Копирует сетевой ответ, не позволяя неизвестному размеру заполнить диск."""
    content_length = source.headers.get("Content-Length")
    try:
        if content_length and int(content_length) > max_bytes:
            raise TooLargeError
    except ValueError:
        pass

    total = 0
    while chunk := source.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise TooLargeError
        target.write(chunk)


def _ig_fetch(
    ydl: yt_dlp.YoutubeDL,
    url: str,
    dest: Path,
    headers: dict | None = None,
    max_bytes: int = MAX_FILE_SIZE,
) -> Path:
    """Качает файл через yt-dlp: так работают те же cookies и прокси, что и у видео."""
    try:
        with ydl.urlopen(yt_dlp.networking.Request(url, headers=headers or {})) as resp:
            ext = {
                "image/jpeg": ".jpg", "image/webp": ".webp", "image/png": ".png", "image/heic": ".heic",
                "video/mp4": ".mp4", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
            }.get((resp.headers.get("Content-Type") or "").split(";")[0].strip(), "")
            dest = dest.with_suffix(ext) if ext else dest
            with dest.open("wb") as f:
                _copy_limited(resp, f, max_bytes)
    except yt_dlp.networking.exceptions.HTTPError as exc:
        # текст «HTTP Error 429: …» подхватят те же разборщики ошибок, что и у yt-dlp
        raise yt_dlp.utils.DownloadError(f"Instagram: {exc}") from exc
    except yt_dlp.networking.exceptions.RequestError as exc:
        raise yt_dlp.utils.DownloadError(f"Instagram: {exc}") from exc
    return dest


def _ig_post(ydl: yt_dlp.YoutubeDL, url: str, workdir: Path) -> dict:
    pk = _ig_media_pk(url)
    if pk is None:
        raise yt_dlp.utils.DownloadError("There is no video in this post")
    raw = _ig_fetch(
        ydl,
        f"https://i.instagram.com/api/v1/media/{pk}/info/",
        workdir / "post.json",
        IG_API_HEADERS,
        MAX_METADATA_SIZE,
    )
    items = json.loads(raw.read_text(encoding="utf-8")).get("items") or []
    if not items:
        raise yt_dlp.utils.DownloadError("There is no video in this post")
    return items[0]


def _ig_music(item: dict) -> dict | None:
    """Музыка поста: откуда качать и какой кусок трека звучит в Instagram."""
    metadata = item.get("music_metadata") or {}
    info = metadata.get("music_info") or {}
    asset = info.get("music_asset_info") or {}
    consumption = info.get("music_consumption_info") or {}
    url = asset.get("progressive_download_url") or asset.get("fast_start_progressive_download_url")
    if url:
        return {
            "url": url,
            "start": (consumption.get("audio_asset_start_time_in_ms") or 0) / 1000,
            "length": (consumption.get("overlap_duration_in_ms") or 0) / 1000,
            "title": asset.get("title") or "",
            "artist": asset.get("display_artist") or "",
        }
    # не трек из библиотеки, а «оригинальный звук» автора: у него своё поле и нет выбранного фрагмента
    sound = metadata.get("original_sound_info") or {}
    url = sound.get("progressive_download_url")
    if url:
        return {
            "url": url,
            "start": 0,
            "length": (sound.get("duration_in_ms") or 0) / 1000,
            "title": sound.get("original_audio_title") or "Оригинальный звук",
            "artist": (sound.get("ig_artist") or {}).get("username") or "",
        }
    return None


def _biggest(versions: list[dict]) -> str | None:
    best = max(versions or [{}], key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))
    return best.get("url")


def _ig_title(item: dict, music: dict | None) -> str:
    author = (item.get("user") or {}).get("username")
    if not music:
        return f"Фото @{author}" if author else "Фото"
    track = " — ".join(part for part in (music["artist"], music["title"]) if part)
    return " · ".join(part for part in (f"Фото @{author}" if author else "Фото", f"🎵 {track}" if track else "") if part)


def _ig_music_only(url: str, workdir: Path, on_progress=None) -> tuple[Path, dict]:
    """«Только звук» для поста с фото: отдаём тот кусок трека, что звучит в посте."""
    workdir.mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(_base_opts("ig")) as ydl:
        item = _ig_post(ydl, url, workdir)
        music = _ig_music(item)
        if not music:
            raise yt_dlp.utils.DownloadError("There is no video in this post")
        track = _ig_fetch(ydl, music["url"], workdir / "music")
    out = workdir / "music.mp3"
    length = music["length"] or SLIDESHOW_MAX_SEC
    _run_ffmpeg(
        ["-ss", str(music["start"]), "-i", str(track), "-t", str(length), "-vn",
         "-c:a", "libmp3lame", "-b:a", "192k",
         "-metadata", f"title={music['title']}", "-metadata", f"artist={music['artist']}", str(out)],
        on_progress, "slideshow",
    )
    return out, {
        "title": _ig_title(item, music), "track": music["title"], "artist": music["artist"],
        "duration": length,
    }


def build_ig_slideshow(url: str, workdir: Path, on_progress=None) -> tuple[Path, dict]:
    """Пост из фото (одно фото или карусель): с музыкой → видео, как его показывает Instagram,
    без музыки → сами фото, info["album"] = [(вид, путь), …].

    yt-dlp такие посты не качает: видео в них нет. Берём слайды и трек из API,
    каждый слайд кодируем в одинаковый отрезок, склеиваем и накладываем музыку.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(_base_opts("ig")) as ydl:
        item = _ig_post(ydl, url, workdir)
        music = _ig_music(item)
        slides = item.get("carousel_media") or [item]
        files = []
        for n, slide in enumerate(slides):
            if on_progress:
                on_progress({"status": "slideshow" if music else "photos"})
            if slide.get("media_type") == 2 and slide.get("video_versions"):
                files.append(("video", _ig_fetch(ydl, _biggest(slide["video_versions"]), workdir / f"s{n:02d}")))
            elif (slide.get("image_versions2") or {}).get("candidates"):
                files.append(("photo", _ig_fetch(ydl, _biggest(slide["image_versions2"]["candidates"]), workdir / f"s{n:02d}")))
        if not files:
            raise yt_dlp.utils.DownloadError("There is no video in this post")
        if not music:
            return workdir, {"title": _ig_title(item, None), "album": _telegram_photos(files, on_progress)}
        track = _ig_fetch(ydl, music["url"], workdir / "music")

    # кадр по пропорциям первого слайда: в карусели Instagram они у всех одинаковые
    first = slides[0]
    src_w = first.get("original_width") or item.get("original_width") or 1080
    src_h = first.get("original_height") or item.get("original_height") or 1350
    width = SLIDESHOW_SIDE
    height = max(2, round(width * src_h / src_w / 2) * 2)
    photos = sum(1 for kind, _ in files if kind == "photo")
    photo_sec = SLIDE_SEC
    if photos == len(files) and music["length"]:
        # одно фото показываем весь фрагмент, несколько — делим его между ними
        photo_sec = music["length"] / photos if photos == 1 else min(SLIDE_MAX_SEC, max(SLIDE_SEC, music["length"] / photos))
    photo_sec = min(photo_sec, SLIDESHOW_MAX_SEC)
    canvas = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,fps={SLIDESHOW_FPS}"
    )
    encode = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-an"]
    segments = []
    total = 0.0
    for n, (kind, path) in enumerate(files):
        if total >= SLIDESHOW_MAX_SEC:
            break
        segment = workdir / f"seg{n:02d}.mp4"
        if kind == "photo":
            # фото декодируем один раз и повторяем кадр: -loop 1 декодировал бы его на каждом
            args = ["-i", str(path), "-vf", f"{canvas},tpad=stop_mode=clone:stop_duration={photo_sec}",
                    "-tune", "stillimage", "-t", str(photo_sec)]
            total += photo_sec
        else:
            length = min(SLIDESHOW_MAX_SEC - total, 60)
            args = ["-i", str(path), "-vf", canvas, "-t", str(length)]
            total += min(length, float(_probe_duration(path) or length))
        _run_ffmpeg([*args, *encode, str(segment)], on_progress, "slideshow")
        segments.append(segment)

    playlist = workdir / "segments.txt"
    playlist.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
    out = workdir / "slideshow.mp4"
    _run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", str(playlist),
         # трек короче слайд-шоу — пускаем по кругу, длиннее — обрезаем по видео
         "-stream_loop", "-1", "-ss", str(music["start"]), "-i", str(track),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
         "-shortest", "-movflags", "+faststart", str(out)],
        on_progress, "slideshow",
    )
    log.info("слайд-шоу: %d слайдов, %s, %dx%d", len(segments), fmt_time(total), width, height)
    return out, {
        "title": _ig_title(item, music), "width": width, "height": height, "duration": total,
    }


def _telegram_photos(files: list[tuple[str, Path]], on_progress=None) -> list[tuple[str, Path]]:
    """Telegram принимает фото в JPEG/PNG; webp и heic из Instagram переводим в JPEG."""
    result = []
    for kind, path in files:
        if kind == "photo" and path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            jpeg = path.with_suffix(".jpg")
            _run_ffmpeg(["-i", str(path), "-frames:v", "1", "-q:v", "2", str(jpeg)], on_progress, "photos")
            path = jpeg
        result.append((kind, path))
    return result


def _probe_duration(path: Path) -> float | None:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(probe.stdout.strip())
    except ValueError:
        return None


def _with_cookie_retry(fn, url: str, workdir: Path, on_progress) -> tuple[Path, dict]:
    """Первая попытка анонимная; если сервис отказал без входа — повторяем с cookies."""
    service = service_of(url_key(url))
    try:
        return fn(url, workdir, on_progress, False)
    except yt_dlp.utils.DownloadError as exc:
        if has_cookie_fallback(service) and is_auth_error(exc):
            log.info(
                "[%s] отказ анонимному запросу (%s…) — повторяю с cookies",
                service, safe_error(exc, 70),
            )
            return fn(url, workdir, on_progress, True)
        raise


def _with_photo_fallback(fn, fallback, url: str, workdir: Path, on_progress) -> tuple[Path, dict]:
    """yt-dlp не нашёл видео в посте Instagram — возможно, это фото с музыкой."""
    try:
        return _with_cookie_retry(fn, url, workdir, on_progress)
    except yt_dlp.utils.DownloadError as exc:
        # музыку API Instagram отдаёт только залогиненным — без cookies и пробовать нечего
        if service_of(url_key(url)) != "ig" or not is_no_video_error(exc) or not use_cookies_for("ig"):
            raise
        log.info("[%s] видео в посте нет — пробую фото с музыкой", url_key(url))
        try:
            return fallback(url, workdir, on_progress)
        except yt_dlp.utils.DownloadError as fallback_exc:
            if is_no_video_error(fallback_exc):
                raise exc from None  # музыки нет — отвечаем исходной ошибкой «только фото»
            raise


def download_video(url: str, workdir: Path, on_progress=None) -> tuple[Path, dict]:
    return _with_photo_fallback(_download_video, build_ig_slideshow, url, workdir, on_progress)


def download_audio(url: str, workdir: Path, on_progress=None) -> tuple[Path, dict]:
    return _with_photo_fallback(_download_audio, _ig_music_only, url, workdir, on_progress)


def _download_audio(url: str, workdir: Path, on_progress, with_cookies: bool) -> tuple[Path, dict]:
    """Забирает только звуковую дорожку и кладёт её в mp3 с тегами и обложкой."""
    workdir.mkdir(parents=True, exist_ok=True)
    service = service_of(url_key(url))
    with yt_dlp.YoutubeDL(_audio_opts(workdir, on_progress, service, with_cookies)) as ydl:
        info = _pick_entry(ydl.extract_info(url, download=True))
    audio = next((f for f in workdir.glob("*.mp3")), None)
    if audio is None:  # постпроцессор не отработал — отдаём что скачалось
        audio = _result_file(info)
    if audio is None or not audio.exists():
        raise yt_dlp.utils.DownloadError("не удалось выделить звуковую дорожку")
    if audio.stat().st_size > MAX_FILE_SIZE:
        raise TooLargeError
    return audio, info


def _download_video(url: str, workdir: Path, on_progress, with_cookies: bool) -> tuple[Path, dict]:
    """Скачивает видео, подбирая качество так, чтобы файл влез в лимит Telegram."""
    service = service_of(url_key(url))
    with yt_dlp.YoutubeDL(_base_opts(service, with_cookies)) as ydl:
        probe = _pick_entry(ydl.extract_info(url, download=False))
    start_height = choose_start_height(probe)
    side = short_side(probe)
    ladder = tuple(h for h in HEIGHT_LADDER if h <= start_height) or (HEIGHT_LADDER[-1],)
    for height in ladder:
        attempt_dir = workdir / f"h{height}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        try:
            with yt_dlp.YoutubeDL(
                _ydl_opts(attempt_dir, height, on_progress, service, with_cookies, side)
            ) as ydl:
                info = _pick_entry(ydl.extract_info(url, download=True))
        except yt_dlp.utils.DownloadError as exc:
            if "max-filesize" in str(exc).lower():
                continue
            raise
        path = _result_file(info)
        if path is None:
            # yt-dlp прервал скачивание (файл превысил лимит) — пробуем качество ниже
            continue
        # после перекодирования файл может потяжелеть — тогда тоже качество ниже
        path = make_ios_compatible(path, info, on_progress)
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
    if is_no_video_error(exc):
        if service == "ig" and not use_cookies_for("ig"):
            # без аккаунта музыку поста не проверить, так что «музыки нет» было бы враньём
            return (
                "🖼 В этом посте нет видео, только фото. Если там есть музыка, собрать из неё "
                "видео можно только через аккаунт Instagram, а у бота он сейчас отключён."
            )
        return "🖼 В этом посте только фото, без видео и музыки. Скачивать нечего."
    if "400" in low and "bad request" in low:
        if ig_session_state == "checkpoint":
            return (
                "🔐 Instagram заблокировал вход в аккаунт бота до подтверждения «Это были вы?». "
                "Владелец уже в курсе — как подтвердит, рилсы снова начнут качаться."
            )
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
        if service == "yt" and (has_cookie_fallback("yt") or use_cookies_for("yt")):
            return (
                "🚧 YouTube отклонил скачивание (HTTP 403) даже с cookies аккаунта. "
                "Возможно, сессия протухла — попробуй позже или выгрузи cookies заново."
            )
        return (
            f"🚧 Сервер {DOWNLOAD_ATTEMPTS} раза подряд отклонил скачивание (HTTP 403) — "
            "похоже, наш IP временно придерживают. Подожди минут десять и пришли ссылку ещё раз."
        )
    if "confirm your age" in low or "age-restricted" in low:
        if has_cookie_fallback("yt") or use_cookies_for("yt"):
            return (
                "🔞 У этого видео возрастное ограничение — YouTube не отдал его "
                "даже с cookies аккаунта. Скачать не получится."
            )
        return (
            "🔞 У этого видео возрастное ограничение — YouTube отдаёт его только "
            "залогиненным, а cookies YouTube у бота нет. Скачать не получится."
        )
    if "404" in low and "not found" in low:
        return "🗑 Похоже, пост удалён или ссылка битая (404)."
    if "sign in to confirm" in low or "not a bot" in low:
        if has_cookie_fallback("yt") or use_cookies_for("yt"):
            return (
                "🤖 YouTube требует вход, и даже cookies аккаунта не убедили его. "
                "Возможно, сессия протухла — попробуй позже или выгрузи cookies заново."
            )
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
    return "💥 Не получилось скачать это видео. Подробности сохранены в логах бота."


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
    if not is_supported(url):
        if is_private:
            await message.reply("Я принимаю только HTTPS-ссылки из YouTube, Instagram, TikTok и X.")
        return
    if not allowed:
        await message.reply(
            "⛔ Эта группа не в списке разрешённых.\n"
            "Пришли /id и добавь id чата в ALLOWED_CHAT_IDS в .env бота."
        )
        return

    key = url_key(url)
    who = describe_sender(message)
    log.info("запрос: %s → %s [%s]", who, safe_url_for_log(url), key)
    requester_id = requester_id_of(message)
    queue_error = await reserve_request(requester_id)
    if queue_error:
        text = (
            "⏳ Сейчас очередь заполнена. Попробуй немного позже."
            if queue_error == "global"
            else "⏳ У тебя уже слишком много запросов в очереди. Дождись их завершения."
        )
        try:
            await message.reply(text)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        return

    try:
        try:
            status = await message.reply("🔍 Смотрю, что за видео…")
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            # бота ограничили в чате — молча пропускаем, иначе каждая ссылка сыпет трейсбеки
            log.warning("не могу писать в чате %s (%s) — пропускаю ссылку", message.chat.id, exc.message)
            return
        lock_entry = retain_url_lock(key)
        try:
            if lock_entry.lock.locked() and key not in file_ids:
                log.info("[%s] уже качается, %s ждёт результат", key, who)
                await status.edit_text("⏳ Это видео уже качается — дождусь и пришлю сюда тоже.")
            async with lock_entry.lock:
                await deliver(message, status, url, key, who, "video")
        finally:
            release_url_lock(key, lock_entry)
    finally:
        await release_request(requester_id)


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


ALBUM_SIZE = 10  # больше Telegram в один альбом не кладёт


async def send_album(message: Message, items: list, caption: str | None) -> list[list[str]]:
    """Шлёт фото и видео альбомами; items — [(вид, файл или file_id), …]. Возвращает file_id для кеша."""
    sent_ids = []
    for start in range(0, len(items), ALBUM_SIZE):
        chunk = items[start:start + ALBUM_SIZE]
        chunk_caption = caption if start == 0 else None
        if len(chunk) == 1:  # альбом из одного элемента Telegram не принимает
            kind, media = chunk[0]
            if kind == "photo":
                send = message.reply_photo(media, caption=chunk_caption)
            else:
                send = message.reply_video(media, caption=chunk_caption, supports_streaming=True)
            messages = [await message.bot(send, request_timeout=UPLOAD_TIMEOUT)]
        else:
            group = [
                InputMediaPhoto(media=media, caption=chunk_caption if n == 0 else None)
                if kind == "photo"
                else InputMediaVideo(media=media, caption=chunk_caption if n == 0 else None, supports_streaming=True)
                for n, (kind, media) in enumerate(chunk)
            ]
            messages = await message.bot(message.reply_media_group(group), request_timeout=UPLOAD_TIMEOUT)
        for sent in messages:
            if sent.photo:
                sent_ids.append(["photo", sent.photo[-1].file_id])  # последний размер — самый большой
            elif sent.video:
                sent_ids.append(["video", sent.video.file_id])
    return sent_ids


async def send_cached(message: Message, status: Message, key: str, kind: str) -> bool:
    """Уже отправляли — Telegram перешлёт файл по file_id мгновенно."""
    entry = cache_get(key, kind)
    if not entry:
        return False
    # в кеше название сырое; без экранирования «Tom & Jerry» роняет отправку в режиме HTML
    caption = html.escape(entry.get("title") or "") or None
    try:
        if entry.get("album"):
            await send_album(message, [tuple(item) for item in entry["album"]], caption)
        elif kind == "audio":
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
    requester_id = requester_id_of(message)
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
                    attempt, DOWNLOAD_ATTEMPTS, safe_url_for_log(url), delay, safe_error(exc),
                )
                await status.edit_text(
                    f"🚧 Сервер отбил скачивание, повторю через {delay} сек… "
                    f"(попытка {attempt + 1} из {DOWNLOAD_ATTEMPTS})"
                )
                await asyncio.sleep(delay)
        job.check()
        title = (info.get("title") or ("Аудио" if kind == "audio" else "Видео"))[:900]
        if info.get("album"):  # пост из одних фото, без музыки
            album = info["album"]
            await status.edit_text(f"📤 Отправляю в Telegram… ({len(album)} шт.)")
            await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
            sent_ids = await send_album(
                message, [(item_kind, FSInputFile(item)) for item_kind, item in album], html.escape(title)
            )
            if sent_ids:
                cache_put_album(key, kind, sent_ids, title)
            if service_of(key) == "ig":
                await set_ig_state(message.bot, "ok")
            log.info(
                "[%s/%s] готово за %s: альбом из %d, для %s",
                key, kind, fmt_time(time.monotonic() - started), len(album), who,
            )
            await status.delete()
            return
        size = path.stat().st_size
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
        if service_of(key) == "ig":  # с проверкой входа Instagram не отдал бы ничего
            await set_ig_state(message.bot, "ok")
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
        # wait_for бросил ждать, но поток yt-dlp ещё качает — остановится на ближайшем хуке
        job.cancelled = True
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
            log.warning("yt-dlp error for %s: %s", safe_url_for_log(url), safe_error(exc))
        if service == "ig":  # отказ Instagram — частый признак, что сессию стёрли или заблокировали
            low = str(exc).lower()
            await check_ig_session(message.bot, url if "400" in low and "bad request" in low else None)
        await show_error(status, friendly_dlp_error(exc, service))
    except TelegramNetworkError as exc:
        log.warning("upload failed for %s: %s", safe_url_for_log(url), safe_error(exc))
        await show_error(
            status,
            "📶 Скачал, но не смог загрузить файл в Telegram — оборвалась сеть или "
            "не хватило таймаута на отправку. Попробуй ещё раз; если повторяется, "
            "увеличь UPLOAD_TIMEOUT в .env.",
        )
    except Exception as exc:
        # Не печатаем traceback: сообщения исключений внешних библиотек могут содержать
        # подписанные URL. Тип и очищенный текст оставляют достаточно данных для диагностики.
        log.error(
            "Не смог обработать [%s] (%s): %s",
            key, type(exc).__name__, safe_error(exc),
        )
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


def validate_security_config() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан: скопируй .env.example в .env и впиши токен от @BotFather.")
    if not ALLOWED_USER_IDS and not ALLOWED_CHAT_IDS:
        raise SystemExit(
            "Доступ закрыт по умолчанию: задай ALLOWED_USER_IDS и/или ALLOWED_CHAT_IDS в .env."
        )

    positive_limits = {
        "MAX_CONCURRENT_DOWNLOADS": MAX_CONCURRENT_DOWNLOADS,
        "MAX_PENDING_REQUESTS": MAX_PENDING_REQUESTS,
        "MAX_REQUESTS_PER_USER": MAX_REQUESTS_PER_USER,
        "MAX_CACHE_ENTRIES": MAX_CACHE_ENTRIES,
        "MAX_FILE_SIZE_MB": MAX_FILE_SIZE_MB,
    }
    invalid = [name for name, value in positive_limits.items() if value < 1]
    if invalid:
        raise SystemExit(f"Настройки должны быть больше нуля: {', '.join(invalid)}")

    if TELEGRAM_API_URL:
        api = urlparse(TELEGRAM_API_URL)
        if (
            api.scheme not in {"http", "https"}
            or api.hostname not in {"127.0.0.1", "::1", "localhost"}
            or api.username
            or api.password
        ):
            raise SystemExit(
                "TELEGRAM_API_URL может указывать только на локальный адрес "
                "127.0.0.1, ::1 или localhost без логина и пароля."
            )

    if COOKIES_FILE:
        cookie_path = Path(COOKIES_FILE)
        if not cookie_path.is_file():
            raise SystemExit("COOKIES_FILE задан, но файл не найден.")
        if os.name != "nt" and cookie_path.stat().st_mode & 0o077:
            log.warning("cookies доступны другим пользователям ОС; установи права 600")
        if ALLOWED_CHAT_IDS:
            log.warning(
                "cookies включены вместе с групповыми чатами: каждый участник разрешённого "
                "чата получает возможности аккаунта из cookies"
            )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    validate_security_config()
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
    session_watcher = asyncio.create_task(watch_ig_session(bot))  # ссылка держит задачу от GC
    try:
        await dp.start_polling(bot)
    finally:
        session_watcher.cancel()


if __name__ == "__main__":
    asyncio.run(main())
