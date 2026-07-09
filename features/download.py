import logging
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID

logger = logging.getLogger(__name__)

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


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value or "").strip("_")
    return cleaned or "plain"


def _normalize_url(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None

    if not re.search(r"^https?://", raw, flags=re.IGNORECASE):
        looks_like_domain = re.match(r"^[\w.-]+\.[a-zA-Z]{2,}([/:?#]|$)", raw)
        if looks_like_domain or re.search(
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


def _extract_platform_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
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


def _targets_for_platform(platform_label: str) -> Tuple[Optional[str], ...]:
    key = (platform_label or "").strip().lower()

    if key in {"tiktok", "instagram", "facebook", "threads", "x", "twitter"}:
        return (
            "Chrome-131:Android-14",
            "Chrome-142:Macos-26",
            "Firefox-135:Macos-14",
            None,
        )

    return (
        None,
        "Chrome-131:Android-14",
        "Chrome-142:Macos-26",
        "Firefox-135:Macos-14",
    )


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
    if not name or not path.is_file():
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

    entries: List[Tuple[float, int, str, Path]] = []

    for path in base.rglob("*"):
        if _is_temp_file(path):
            continue

        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            logger.debug("Skipping unreadable path: %s", path, exc_info=True)
            continue

        if st.st_size <= 0:
            continue

        entries.append((st.st_mtime, st.st_size, path.name.lower(), path))

    entries.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in entries]


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


def _make_caption(title: str, platform: str, path: Path, index: int, total: int) -> str:
    caption = (
        f"📥 <b>{_escape(_truncate(title, 120))}</b>\n"
        f"Platform: <b>{_escape(platform)}</b>\n"
        f"File: <code>{_escape(_truncate(path.name, 100))}</code>\n"
        f"({index}/{total})"
    )
    return _truncate(caption, 900)


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
    if not path.exists() or path.stat().st_size <= 0:
        return False

    kind, mime = _guess_media_kind(path)
    logger.debug("Sending file path=%s kind=%s mime=%s", path, kind, mime)

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


def _tail_text(text: Optional[str], max_chars: int = 1200) -> str:
    data = (text or "").strip()
    if not data:
        return ""
    if len(data) <= max_chars:
        return data
    return data[-max_chars:]


def _build_cli_command(url: str, impersonate: Optional[str]) -> List[str]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--format",
        "bv*+ba/best",
        "--merge-output-format",
        "mp4",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "--extractor-retries",
        "3",
        "--socket-timeout",
        "25",
        "--concurrent-fragments",
        "1",
        "--restrict-filenames",
        "--output",
        "%(title).200B-%(id)s.%(ext)s",
    ]

    if impersonate:
        cmd.extend(["--impersonate", impersonate])

    cmd.append(url)
    return cmd


def _run_one_attempt(url: str, attempt_dir: Path, impersonate: Optional[str], platform_label: str):
    env = os.environ.copy()
    env["TMPDIR"] = str(attempt_dir)
    env["TEMP"] = str(attempt_dir)
    env["TMP"] = str(attempt_dir)

    cmd = _build_cli_command(url, impersonate)
    logger.info(
        "Running yt-dlp attempt platform=%s impersonate=%r cwd=%s",
        platform_label,
        impersonate,
        attempt_dir,
    )

    result = subprocess.run(
        cmd,
        cwd=str(attempt_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
    )

    if result.stdout:
        logger.debug("yt-dlp stdout (tail): %s", _tail_text(result.stdout))
    if result.stderr:
        logger.debug("yt-dlp stderr (tail): %s", _tail_text(result.stderr))

    return result


def _download_with_retries(url: str, tmpdir: str):
    platform_label = _extract_platform_from_url(url)
    attempt_errors: List[str] = []
    targets = _targets_for_platform(platform_label)

    for attempt_no, impersonate in enumerate(targets, start=1):
        attempt_dir = Path(tmpdir) / f"attempt_{attempt_no}_{_slugify(impersonate or 'plain')}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        try:
            result = _run_one_attempt(url, attempt_dir, impersonate, platform_label)
            files = _scan_downloaded_files(str(attempt_dir))

            if files:
                if result.returncode != 0:
                    logger.warning(
                        "yt-dlp returned non-zero but files were found. rc=%s platform=%s impersonate=%r",
                        result.returncode,
                        platform_label,
                        impersonate,
                    )
                return platform_label, files

            error_tail = _tail_text(result.stderr or result.stdout)
            if result.returncode == 0:
                raise RuntimeError(
                    "Media tidak ditemukan dari tautan ini."
                    + (f"\n{error_tail}" if error_tail else "")
                )

            raise RuntimeError(
                f"yt-dlp gagal (rc={result.returncode})."
                + (f"\n{error_tail}" if error_tail else "")
            )

        except subprocess.TimeoutExpired:
            msg = f"attempt={attempt_no} impersonate={impersonate!r} error=TimeoutExpired"
            attempt_errors.append(msg)
            logger.exception("Download timeout: %s", msg)
            time.sleep(0.5)
        except Exception as exc:
            msg = f"attempt={attempt_no} impersonate={impersonate!r} error={exc.__class__.__name__}: {exc}"
            attempt_errors.append(msg)
            logger.exception("Download attempt failed: %s", msg)
            time.sleep(0.5)

    raise RuntimeError(
        "Gagal mengunduh media setelah beberapa percobaan.\n"
        + "\n".join(attempt_errors[-5:])
    )


def process_downloader_url(bot, chat_id: int, url: str):
    with tempfile.TemporaryDirectory(prefix="universal_downloader_") as tmpdir:
        try:
            platform_label = _extract_platform_from_url(url)
            _safe_send_message(
                bot,
                chat_id,
                "⏳ <b>Memulai unduhan</b>\n"
                f"Platform: <b>{_escape(platform_label)}</b>\n"
                f"URL: <code>{_escape(_truncate(url, 180))}</code>\n\n"
                "Menyiapkan koneksi...",
                parse_mode="HTML",
            )

            platform_label, paths = _download_with_retries(url, tmpdir)

            if not paths:
                raise RuntimeError("Media tidak ditemukan dari tautan ini.")

            _safe_send_message(
                bot,
                chat_id,
                "✅ <b>File ditemukan</b>\n"
                f"Platform: <b>{_escape(platform_label)}</b>\n"
                f"Jumlah file: <b>{len(paths)}</b>\n\n"
                "Mengirim ke Telegram...",
                parse_mode="HTML",
            )

            sent = 0
            total = len(paths)

            title = _truncate(paths[0].stem, 120) if paths else "Media"

            for index, path in enumerate(paths, start=1):
                caption = _make_caption(title, platform_label, path, index, total)
                if _send_file_with_fallback(bot, chat_id, path, caption):
                    sent += 1
                else:
                    logger.error("Failed to send file after all fallbacks: %s", path)

            if sent == 0:
                raise RuntimeError("Tidak ada file yang berhasil dikirim ke Telegram.")

            if sent < total:
                summary = (
                    "✅ <b>Download selesai</b>\n\n"
                    f"Berhasil mengirim <b>{sent}/{total}</b> file.\n"
                    "Sebagian file gagal dikirim."
                )
            else:
                summary = (
                    "✅ <b>Download selesai</b>\n\n"
                    f"Berhasil mengirim <b>{sent}</b> file."
                )

            _safe_send_message(
                bot,
                chat_id,
                summary + "\nSilakan kirim tautan lain.",
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
