import logging
import mimetypes
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DOWNLOADER_STATE: Dict[int, bool] = {}

URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "threads": "Threads",
    "x": "X",
    "twitter": "X",
    "pinterest": "Pinterest",
    "reddit": "Reddit",
    "vimeo": "Vimeo",
    "dailymotion": "Dailymotion",
    "soundcloud": "SoundCloud",
    "mediafire": "MediaFire",
    "generic": "Platform",
}

# yt-dlp supports impersonation via CLIENT[:OS].
# The targets below are intentionally conservative and ordered from newer to broader fallbacks.
IMPERSONATE_TARGETS: Tuple[str, ...] = (
    "Chrome-142:Macos-26",
    "Chrome-131:Android-14",
    "Chrome-124:Macos-14",
    "Firefox-135:Macos-14",
    "Safari-18.4:Ios-18.4",
    "Chrome-101:Windows-10",
    "Safari-17.0:Macos-14",
    "",
)

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".flv",
    ".wmv",
    ".m4v",
    ".ts",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".3gpp",
    ".ogv",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
    ".aiff",
    ".alac",
    ".amr",
    ".webm",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".avif",
}

TEMP_SUFFIX_MARKERS = (
    ".part",
    ".tmp",
    ".json",
    ".ytdl",
    ".temp",
)


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def clear_pending(user_id: int):
    DOWNLOADER_STATE.pop(user_id, None)


def _escape(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _truncate(value: Any, limit: int = 900) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _normalize_url(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None

    if not re.search(r"^https?://", raw, flags=re.IGNORECASE):
        if re.search(
            r"\b(?:instagram\.com|tiktok\.com|youtu\.be|youtube\.com|facebook\.com|"
            r"x\.com|twitter\.com|threads\.net|pinterest\.com|reddit\.com|"
            r"vimeo\.com|dailymotion\.com|soundcloud\.com|mediafire\.com)\b",
            raw,
            flags=re.IGNORECASE,
        ):
            raw = "https://" + raw.lstrip("/")

    match = URL_RE.search(raw)
    if not match:
        return None

    return match.group(0).rstrip(").,]}>\"'")


def _pretty_platform(info: dict, url: str) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").strip().lower()
    if extractor in PLATFORM_LABELS:
        return PLATFORM_LABELS[extractor]

    host = urlparse(url).netloc.lower().lstrip("www.")
    if host.startswith("m."):
        host = host[2:]

    host_map = [
        ("youtu.be", "YouTube"),
        ("youtube.com", "YouTube"),
        ("instagram.com", "Instagram"),
        ("tiktok.com", "TikTok"),
        ("facebook.com", "Facebook"),
        ("threads.net", "Threads"),
        ("x.com", "X"),
        ("twitter.com", "X"),
        ("pinterest.com", "Pinterest"),
        ("reddit.com", "Reddit"),
        ("vimeo.com", "Vimeo"),
        ("dailymotion.com", "Dailymotion"),
        ("soundcloud.com", "SoundCloud"),
        ("mediafire.com", "MediaFire"),
    ]

    for key, label in host_map:
        if key in host:
            return label

    return "Platform"


def _utilitas_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📥 Universal Downloader", callback_data="util:download"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _downloader_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("⬅️ Kembali ke Utilitas", callback_data="util:back"))
    return kb


def _safe_send_message(bot, chat_id: int, text: str, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.exception("Failed to send message to chat_id=%s", chat_id)
        return None


def _safe_edit_message(bot, chat_id: int, message_id: int, text: str, **kwargs):
    try:
        return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception:
        logger.debug("Edit message failed for chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)
        return None


def _safe_answer_callback(bot, callback_id: str, text: Optional[str] = None, show_alert: bool = False):
    try:
        if text is None:
            return bot.answer_callback_query(callback_id)
        return bot.answer_callback_query(callback_id, text, show_alert=show_alert)
    except Exception:
        logger.debug("Callback answer failed for callback_id=%s", callback_id, exc_info=True)
        return None


def show_utilitas_home(bot, chat_id: int):
    _safe_send_message(
        bot,
        chat_id,
        "🛠️ <b>Utilitas</b>\n\nPilih alat yang ingin digunakan.",
        reply_markup=_utilitas_keyboard(),
        parse_mode="HTML",
    )


def show_downloader_home(bot, chat_id: int):
    _safe_send_message(
        bot,
        chat_id,
        "📥 <b>Universal Downloader</b>\n\n"
        "Kirim tautan dari platform apa pun yang didukung.\n"
        "Bot akan mendeteksi platform secara otomatis.",
        reply_markup=_downloader_keyboard(),
        parse_mode="HTML",
    )


def _is_temp_file(path: Path) -> bool:
    name = path.name.lower()
    if not name:
        return True
    if not path.is_file():
        return True
    try:
        if path.stat().st_size <= 0:
            return True
    except OSError:
        return True
    if name.endswith(".part") or ".part." in name:
        return True
    for marker in TEMP_SUFFIX_MARKERS:
        if name.endswith(marker):
            return True
    return False


def _scan_downloaded_files(root_dir: str) -> List[Path]:
    base = Path(root_dir)
    if not base.exists():
        return []

    files: List[Path] = []
    for path in base.rglob("*"):
        if _is_temp_file(path):
            continue
        try:
            if path.stat().st_size > 0:
                files.append(path)
        except FileNotFoundError:
            continue
        except OSError:
            logger.debug("Skipping unreadable path: %s", path, exc_info=True)

    files.sort(key=lambda p: (p.stat().st_mtime if p.exists() else 0.0, p.name.lower()), reverse=True)
    return files


def _make_caption(title: str, platform: str, path: Path, index: int, total: int) -> str:
    caption = (
        f"📥 <b>{_escape(_truncate(title, 120))}</b>\n"
        f"Platform: <b>{_escape(platform)}</b>\n"
        f"File: <code>{_escape(_truncate(path.name, 100))}</code>\n"
        f"({index}/{total})"
    )
    return _truncate(caption, 900)


def _guess_media_kind(path: Path) -> Tuple[str, Optional[str]]:
    name = path.name.lower()
    ext = path.suffix.lower()
    mime, _ = mimetypes.guess_type(path.name)

    if ext == ".gif" or mime == "image/gif" or name.endswith(".gifv"):
        return "animation", mime

    if mime is not None:
        if mime.startswith("image/"):
            return "photo", mime
        if mime.startswith("video/"):
            return "video", mime
        if mime.startswith("audio/"):
            return "audio", mime

    if ext in IMAGE_EXTENSIONS:
        return "photo", mime

    if ext in VIDEO_EXTENSIONS:
        return "video", mime

    if ext in AUDIO_EXTENSIONS:
        return "audio", mime

    return "document", mime


def _send_document(bot, chat_id: int, fh, caption: str):
    return bot.send_document(chat_id, document=fh, caption=caption, parse_mode="HTML")


def _send_photo(bot, chat_id: int, fh, caption: str):
    return bot.send_photo(chat_id, photo=fh, caption=caption, parse_mode="HTML")


def _send_video(bot, chat_id: int, fh, caption: str):
    return bot.send_video(chat_id, video=fh, caption=caption, parse_mode="HTML", supports_streaming=True)


def _send_audio(bot, chat_id: int, fh, caption: str):
    return bot.send_audio(chat_id, audio=fh, caption=caption, parse_mode="HTML")


def _send_animation(bot, chat_id: int, fh, caption: str):
    return bot.send_animation(chat_id, animation=fh, caption=caption, parse_mode="HTML")


def _send_file_with_fallback(bot, chat_id: int, path: Path, caption: str) -> bool:
    if not path.exists():
        return False

    kind, mime = _guess_media_kind(path)
    logger.info("Sending file kind=%s mime=%s path=%s", kind, mime, path)

    with open(path, "rb") as fh:
        try:
            if kind == "animation":
                try:
                    _send_animation(bot, chat_id, fh, caption)
                    return True
                except Exception:
                    logger.debug("send_animation failed for %s", path, exc_info=True)
                    fh.seek(0)
                    _send_document(bot, chat_id, fh, caption)
                    return True

            if kind == "photo":
                try:
                    _send_photo(bot, chat_id, fh, caption)
                    return True
                except Exception:
                    logger.debug("send_photo failed for %s", path, exc_info=True)
                    fh.seek(0)
                    _send_document(bot, chat_id, fh, caption)
                    return True

            if kind == "video":
                try:
                    _send_video(bot, chat_id, fh, caption)
                    return True
                except Exception:
                    logger.debug("send_video failed for %s", path, exc_info=True)
                    fh.seek(0)
                    _send_document(bot, chat_id, fh, caption)
                    return True

            if kind == "audio":
                try:
                    _send_audio(bot, chat_id, fh, caption)
                    return True
                except Exception:
                    logger.debug("send_audio failed for %s", path, exc_info=True)
                    fh.seek(0)
                    _send_document(bot, chat_id, fh, caption)
                    return True

            _send_document(bot, chat_id, fh, caption)
            return True

        except Exception:
            logger.exception("Failed to send file path=%s", path)
            try:
                fh.seek(0)
                _send_document(bot, chat_id, fh, caption)
                return True
            except Exception:
                logger.exception("Document fallback also failed for path=%s", path)
                return False


class DownloadProgressReporter:
    def __init__(self, bot, chat_id: int, source_label: str):
        self.bot = bot
        self.chat_id = chat_id
        self.source_label = source_label
        self.message = None
        self.message_id: Optional[int] = None
        self.last_update = 0.0
        self.started_at = time.monotonic()
        self.finished = False

    def start(self, url: str):
        text = (
            f"⏳ <b>Memulai unduhan</b>\n"
            f"Platform: <b>{_escape(self.source_label)}</b>\n"
            f"URL: <code>{_escape(_truncate(url, 160))}</code>\n\n"
            f"Menyiapkan koneksi..."
        )
        self.message = _safe_send_message(self.bot, self.chat_id, text, parse_mode="HTML")
        if self.message is not None:
            self.message_id = getattr(self.message, "message_id", None)

    def _format_progress_text(self, data: dict) -> str:
        status = str(data.get("status") or "").lower()
        filename = data.get("filename") or data.get("tmpfilename") or ""
        downloaded_bytes = data.get("downloaded_bytes")
        total_bytes = data.get("total_bytes") or data.get("total_bytes_estimate")
        speed = data.get("speed")
        eta = data.get("eta")

        parts = [
            "⬇️ <b>Sedang mengunduh</b>",
            f"Platform: <b>{_escape(self.source_label)}</b>",
        ]

        if filename:
            parts.append(f"File: <code>{_escape(_truncate(Path(str(filename)).name, 120))}</code>")

        if total_bytes and isinstance(downloaded_bytes, (int, float)):
            try:
                pct = max(0.0, min(100.0, (float(downloaded_bytes) / float(total_bytes)) * 100.0))
                parts.append(f"Progress: <b>{pct:.1f}%</b>")
            except Exception:
                logger.debug("Failed to compute percentage", exc_info=True)

        if speed:
            try:
                parts.append(f"Speed: <b>{_escape(_format_size(float(speed)))}/s</b>")
            except Exception:
                logger.debug("Failed to format speed", exc_info=True)

        if eta is not None:
            try:
                eta_int = int(float(eta))
                parts.append(f"ETA: <b>{eta_int}s</b>")
            except Exception:
                logger.debug("Failed to format ETA", exc_info=True)

        if status == "finished":
            parts[0] = "✅ <b>Unduhan selesai, memproses file...</b>"

        elapsed = max(0.0, time.monotonic() - self.started_at)
        parts.append(f"Elapsed: <b>{elapsed:.1f}s</b>")

        return "\n".join(parts)

    def hook(self, data: dict):
        try:
            status = str(data.get("status") or "").lower()
            now = time.monotonic()
            should_update = status == "finished" or (now - self.last_update) >= 2.0
            if not should_update:
                return

            self.last_update = now
            if self.message_id is None:
                return

            text = self._format_progress_text(data)
            _safe_edit_message(self.bot, self.chat_id, self.message_id, text, parse_mode="HTML")
            if status == "finished":
                self.finished = True
        except Exception:
            logger.debug("Progress hook failure", exc_info=True)

    def finalize(self, text: str):
        if self.message_id is None:
            return
        _safe_edit_message(self.bot, self.chat_id, self.message_id, text, parse_mode="HTML")


def _format_size(value: float) -> str:
    if value < 1024:
        return f"{value:.0f} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    return f"{value / (1024 * 1024 * 1024):.1f} GB"


def _build_ydl_options(tmpdir: str, impersonate: Optional[str], reporter: DownloadProgressReporter) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "format": "bv*+ba/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "ignoreerrors": False,
        "restrictfilenames": True,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 1,
        "overwrites": True,
        "outtmpl": os.path.join(tmpdir, "%(title).200B-%(id)s.%(ext)s"),
        "paths": {"home": tmpdir},
        "progress_hooks": [reporter.hook],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            )
        },
    }

    if impersonate is not None:
        options["impersonate"] = impersonate

    return options


def _download_once(url: str, tmpdir: str, bot, chat_id: int, source_label: str, impersonate: Optional[str]):
    try:
        from yt_dlp import YoutubeDL
        try:
            from yt_dlp.utils import DownloadError, ExtractorError, PostProcessingError, YoutubeDLError
        except Exception:
            from yt_dlp.utils import DownloadError  # type: ignore
            ExtractorError = DownloadError  # type: ignore[assignment]
            PostProcessingError = DownloadError  # type: ignore[assignment]
            YoutubeDLError = DownloadError  # type: ignore[assignment]
    except ImportError as exc:
        raise RuntimeError("Paket yt-dlp belum terpasang di VPS.") from exc

    reporter = DownloadProgressReporter(bot, chat_id, source_label)
    reporter.start(url)

    options = _build_ydl_options(tmpdir, impersonate, reporter)

    with YoutubeDL(options) as ydl:
        try:
            info = ydl.extract_info(url, download=True) or {}
            reporter.finalize(
                f"✅ <b>Unduhan selesai</b>\n"
                f"Platform: <b>{_escape(source_label)}</b>\n"
                f"Memindai file hasil unduhan..."
            )
            return info
        except (DownloadError, ExtractorError, PostProcessingError, YoutubeDLError) as exc:
            logger.exception("yt-dlp failure with impersonate=%r", impersonate)
            raise exc
        except Exception as exc:
            logger.exception("Unexpected yt-dlp failure with impersonate=%r", impersonate)
            raise exc


def _extract_platform_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().lstrip("www.")
    if host.startswith("m."):
        host = host[2:]

    host_map = [
        ("youtu.be", "YouTube"),
        ("youtube.com", "YouTube"),
        ("instagram.com", "Instagram"),
        ("tiktok.com", "TikTok"),
        ("facebook.com", "Facebook"),
        ("threads.net", "Threads"),
        ("x.com", "X"),
        ("twitter.com", "X"),
        ("pinterest.com", "Pinterest"),
        ("reddit.com", "Reddit"),
        ("vimeo.com", "Vimeo"),
        ("dailymotion.com", "Dailymotion"),
        ("soundcloud.com", "SoundCloud"),
        ("mediafire.com", "MediaFire"),
    ]

    for key, label in host_map:
        if key in host:
            return label
    return "Platform"


def _download_with_retries(bot, chat_id: int, url: str, tmpdir: str):
    source_hint = _extract_platform_from_url(url)
    attempt_errors: List[str] = []

    for outer_attempt in range(1, 4):
        for impersonate in IMPERSONATE_TARGETS:
            try:
                info = _download_once(
                    url=url,
                    tmpdir=tmpdir,
                    bot=bot,
                    chat_id=chat_id,
                    source_label=source_hint,
                    impersonate=impersonate if impersonate != "" else "",
                )
                files = _scan_downloaded_files(tmpdir)
                if files:
                    return info, files, source_hint
                raise RuntimeError("Media tidak ditemukan dari tautan ini.")
            except Exception as exc:
                msg = f"attempt={outer_attempt} impersonate={impersonate!r} error={exc.__class__.__name__}: {exc}"
                attempt_errors.append(msg)
                logger.exception("Download attempt failed: %s", msg)
                time.sleep(min(3.0, 0.8 * outer_attempt))

    raise RuntimeError(
        "Gagal mengunduh media setelah beberapa percobaan.\n"
        + "\n".join(attempt_errors[-5:])
    )


def process_downloader_url(bot, chat_id: int, url: str):
    with tempfile.TemporaryDirectory(prefix="universal_downloader_") as tmpdir:
        try:
            _safe_send_message(
                bot,
                chat_id,
                "⏳ <b>Memproses tautan...</b>\n"
                "Bot sedang menyiapkan unduhan dan mendeteksi platform.",
                parse_mode="HTML",
            )

            info, paths, source_label = _download_with_retries(bot, chat_id, url, tmpdir)
            platform = _pretty_platform(info, url) if isinstance(info, dict) else source_label
            title = str(
                (info or {}).get("title")
                or (info or {}).get("playlist_title")
                or Path(urlparse(url).path).name
                or "Media"
            ).strip()

            if not paths:
                raise RuntimeError("Media tidak ditemukan dari tautan ini.")

            _safe_send_message(
                bot,
                chat_id,
                "✅ <b>File ditemukan</b>\n"
                f"Platform: <b>{_escape(platform)}</b>\n"
                f"Jumlah file: <b>{len(paths)}</b>\n\n"
                "Mengirim ke Telegram...",
                parse_mode="HTML",
            )

            sent = 0
            total = len(paths)
            for index, path in enumerate(paths, start=1):
                caption = _make_caption(title, platform, path, index, total)
                ok = _send_file_with_fallback(bot, chat_id, path, caption)
                if ok:
                    sent += 1
                else:
                    logger.error("Failed to send file after all fallbacks: %s", path)

            if sent == 0:
                raise RuntimeError("Tidak ada file yang berhasil dikirim ke Telegram.")

            _safe_send_message(
                bot,
                chat_id,
                "✅ <b>Download selesai</b>\n\n"
                f"Berhasil mengirim <b>{sent}</b> file.\n"
                "Silakan kirim tautan lain.",
                reply_markup=_downloader_keyboard(),
                parse_mode="HTML",
            )

        except Exception as exc:
            logger.exception("process_downloader_url failed for url=%s", url)
            _safe_send_message(
                bot,
                chat_id,
                "⚠️ <b>Gagal mengunduh media.</b>\n\n"
                f"{_escape(str(exc))}",
                reply_markup=_downloader_keyboard(),
                parse_mode="HTML",
            )


def register_download(bot):
    @bot.callback_query_handler(func=lambda call: getattr(call, "data", None) == "main:utilitas")
    def open_utilitas(call):
        if not allowed(call.from_user.id):
            _safe_answer_callback(bot, call.id, "Akses ditolak")
            return
        clear_pending(call.from_user.id)
        show_utilitas_home(bot, call.message.chat.id)
        _safe_answer_callback(bot, call.id)

    @bot.message_handler(commands=["utilitas"])
    def cmd_utilitas(message):
        if not allowed(message.from_user.id):
            return
        clear_pending(message.from_user.id)
        show_utilitas_home(bot, message.chat.id)

    @bot.message_handler(commands=["downloader"])
    def cmd_downloader(message):
        if not allowed(message.from_user.id):
            return
        clear_pending(message.from_user.id)
        DOWNLOADER_STATE[message.from_user.id] = True
        show_downloader_home(bot, message.chat.id)

    @bot.callback_query_handler(func=lambda call: getattr(call, "data", None) == "util:download")
    def open_downloader(call):
        if not allowed(call.from_user.id):
            _safe_answer_callback(bot, call.id, "Akses ditolak")
            return
        clear_pending(call.from_user.id)
        DOWNLOADER_STATE[call.from_user.id] = True
        show_downloader_home(bot, call.message.chat.id)
        _safe_answer_callback(bot, call.id)

    @bot.callback_query_handler(func=lambda call: getattr(call, "data", None) == "util:back")
    def back_to_utilitas(call):
        if not allowed(call.from_user.id):
            _safe_answer_callback(bot, call.id, "Akses ditolak")
            return
        clear_pending(call.from_user.id)
        DOWNLOADER_STATE.pop(call.from_user.id, None)
        show_utilitas_home(bot, call.message.chat.id)
        _safe_answer_callback(bot, call.id)

    @bot.message_handler(
        content_types=["text"],
        func=lambda m: allowed(m.from_user.id)
        and m.from_user.id in DOWNLOADER_STATE
        and bool(getattr(m, "text", None))
        and not m.text.startswith("/"),
    )
    def downloader_text_handler(message):
        url = _normalize_url(message.text or "")
        if not url:
            _safe_send_message(
                bot,
                message.chat.id,
                "Silakan kirim tautan yang valid.",
                reply_markup=_downloader_keyboard(),
                parse_mode="HTML",
            )
            return

        process_downloader_url(bot, message.chat.id, url)

    @bot.message_handler(
        content_types=["photo", "video", "document", "audio", "voice", "sticker", "animation", "location", "contact"],
    )
    def downloader_non_text(message):
        if not allowed(message.from_user.id):
            return
        if message.from_user.id not in DOWNLOADER_STATE:
            return

        _safe_send_message(
            bot,
            message.chat.id,
            "Silakan kirim tautan sebagai teks.",
            reply_markup=_downloader_keyboard(),
            parse_mode="HTML",
        )
