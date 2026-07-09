import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COOKIES_FILE = PROJECT_ROOT / "cookies.txt"
FFMPEG_PATH = "/usr/bin/ffmpeg"

DOWNLOADER_STATE: Dict[int, bool] = {}
REGISTERED_DOWNLOAD_HANDLERS = False

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

IMPERSONATE_TARGETS: Tuple[Optional[str], ...] = (
    "Chrome-142:Macos-26",
    "Chrome-131:Android-14",
    "Chrome-124:Macos-14",
    "Firefox-135:Macos-14",
    "Safari-18.4:Ios-18.4",
    "Chrome-101:Windows-10",
    "Safari-17.0:Macos-14",
    None,
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
    ".weba",
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

TEMP_SUFFIX_MARKERS = (".part", ".tmp", ".json", ".ytdl", ".temp")


def report_local_error(where: str, exc: Exception):
    print(f"🚨 Downloader error di [{where}]: {exc}")
    print(traceback.format_exc())
    logger.error("Downloader error di [%s]: %s", where, exc)
    logger.debug("Traceback for [%s]", where, exc_info=True)


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def clear_pending(user_id: int):
    DOWNLOADER_STATE.pop(user_id, None)


def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(value: Any, limit: int = 900) -> str:
    text = "" if value is None else str(value)
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

    return match.group(0).rstrip(").,]}>'\"")


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


def _pretty_platform(info: dict, url: str) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").strip().lower()
    if extractor in PLATFORM_LABELS:
        return PLATFORM_LABELS[extractor]
    return _extract_platform_from_url(url)


def _utilitas_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📥 Universal Downloader", callback_data="util:download"),
        InlineKeyboardButton("🖥️ Monitor Server",callback_data="main:monitor"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _downloader_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("⬅️ Kembali ke Utilitas", callback_data="util:back"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
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
        except (FileNotFoundError, OSError):
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


def _format_size(value: float) -> str:
    if value < 1024:
        return f"{value:.0f} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    return f"{value / (1024 * 1024 * 1024):.1f} GB"


def _format_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except Exception:
        return "-"

    if total <= 0:
        return "-"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _extract_metadata(info: dict, platform_label: str, url: str) -> str:
    title = str(info.get("title") or info.get("fulltitle") or "Media").strip()
    uploader = (
        info.get("uploader")
        or info.get("channel")
        or info.get("creator")
        or info.get("artist")
        or info.get("uploader_id")
        or "-"
    )
    duration = _format_duration(info.get("duration"))
    n_entries = info.get("n_entries")
    if n_entries is None and isinstance(info.get("entries"), list):
        n_entries = len(info.get("entries") or [])

    lines = [
        "📌 <b>Metadata</b>",
        f"Platform: <b>{_escape(platform_label)}</b>",
        f"Judul: <b>{_escape(_truncate(title, 140))}</b>",
        f"Creator: <b>{_escape(_truncate(uploader, 120))}</b>",
        f"Durasi: <b>{_escape(duration)}</b>",
    ]

    if n_entries:
        lines.append(f"Items: <b>{_escape(str(n_entries))}</b>")

    if info.get("webpage_url"):
        lines.append(f"URL: <code>{_escape(_truncate(info.get('webpage_url'), 180))}</code>")
    else:
        lines.append(f"URL: <code>{_escape(_truncate(url, 180))}</code>")

    if info.get("thumbnail"):
        lines.append("Thumbnail: tersedia")

    return "\n".join(lines)


def _build_dump_json_cmd(impersonate: Optional[str], instagram_cookies: bool) -> List[str]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--restrict-filenames",
    ]

    if instagram_cookies and COOKIES_FILE.exists():
        cmd.extend(["--cookies", str(COOKIES_FILE)])

    if impersonate:
        cmd.extend(["--impersonate", impersonate])

    return cmd


def _build_download_cmd(output_dir: str, impersonate: Optional[str], instagram_cookies: bool) -> List[str]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--restrict-filenames",
        "--windows-filenames",
        "--merge-output-format",
        "mp4",
        "--ffmpeg-location",
        FFMPEG_PATH,
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
        "--output",
        os.path.join(output_dir, "%(title).200B-%(id)s.%(ext)s"),
    ]

    if instagram_cookies and COOKIES_FILE.exists():
        cmd.extend(["--cookies", str(COOKIES_FILE)])

    if impersonate:
        cmd.extend(["--impersonate", impersonate])

    return cmd


def _run_cli(cmd: List[str], cwd: Optional[str] = None, timeout: int = 1200):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result


def _parse_json_stdout(stdout: str) -> dict:
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        raise RuntimeError(f"Gagal membaca metadata JSON dari yt-dlp. Output: {_truncate(text, 1200)}")


def _metadata_attempt_order(preferred: Optional[str]) -> List[Optional[str]]:
    ordered: List[Optional[str]] = []
    if preferred is not None:
        ordered.append(preferred)
    for target in IMPERSONATE_TARGETS:
        if target not in ordered:
            ordered.append(target)
    return ordered


def _download_attempt_order(preferred: Optional[str]) -> List[Optional[str]]:
    ordered: List[Optional[str]] = []
    if preferred is not None:
        ordered.append(preferred)
    for target in IMPERSONATE_TARGETS:
        if target not in ordered:
            ordered.append(target)
    return ordered


def _fetch_metadata(url: str, tmpdir: str) -> Tuple[dict, str, Optional[str]]:
    platform_label = _extract_platform_from_url(url)
    instagram_cookies = platform_label == "Instagram"
    last_error: Optional[Exception] = None

    for attempt_no, impersonate in enumerate(_metadata_attempt_order(None), start=1):
        try:
            cmd = _build_dump_json_cmd(impersonate, instagram_cookies)
            cmd.append(url)

            result = _run_cli(cmd, cwd=tmpdir, timeout=600)
            if result.returncode != 0:
                tail = _truncate((result.stderr or result.stdout or "").strip(), 2000)
                raise RuntimeError(
                    f"Metadata yt-dlp gagal (rc={result.returncode})."
                    + (f"\n{tail}" if tail else "")
                )

            info = _parse_json_stdout(result.stdout)
            if info:
                return info, platform_label, impersonate

            raise RuntimeError("Metadata kosong dari yt-dlp.")
        except Exception as exc:
            last_error = exc
            report_local_error(f"fetch_metadata_attempt_{attempt_no}", exc)
            time.sleep(0.3)

    if last_error is not None:
        return {}, platform_label, None
    return {}, platform_label, None


def _run_download_attempt(
    url: str,
    attempt_dir: Path,
    impersonate: Optional[str],
    platform_label: str,
    instagram_cookies: bool,
):
    attempt_dir.mkdir(parents=True, exist_ok=True)
    cmd = _build_download_cmd(str(attempt_dir), impersonate, instagram_cookies)
    cmd.append(url)

    logger.info(
        "Running yt-dlp download platform=%s impersonate=%r dir=%s",
        platform_label,
        impersonate,
        attempt_dir,
    )

    result = _run_cli(cmd, cwd=str(attempt_dir), timeout=1200)

    if result.stdout:
        logger.debug("yt-dlp stdout (tail): %s", _truncate(result.stdout, 2000))
    if result.stderr:
        logger.debug("yt-dlp stderr (tail): %s", _truncate(result.stderr, 2000))

    return result


def _download_with_retries(url: str, tmpdir: str, preferred_impersonate: Optional[str] = None):
    platform_label = _extract_platform_from_url(url)
    instagram_cookies = platform_label == "Instagram"
    attempt_errors: List[str] = []

    for outer_attempt in range(1, 3):
        for impersonate in _download_attempt_order(preferred_impersonate):
            attempt_dir = Path(tmpdir) / f"attempt_{outer_attempt}_{_slugify(str(impersonate or 'plain'))}"

            try:
                result = _run_download_attempt(
                    url=url,
                    attempt_dir=attempt_dir,
                    impersonate=impersonate,
                    platform_label=platform_label,
                    instagram_cookies=instagram_cookies,
                )
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

                tail = _truncate((result.stderr or result.stdout or "").strip(), 2000)
                if result.returncode == 0:
                    raise RuntimeError(
                        "Media tidak ditemukan dari tautan ini."
                        + (f"\n{tail}" if tail else "")
                    )

                raise RuntimeError(
                    f"yt-dlp gagal (rc={result.returncode})."
                    + (f"\n{tail}" if tail else "")
                )

            except subprocess.TimeoutExpired:
                msg = f"attempt={outer_attempt} impersonate={impersonate!r} error=TimeoutExpired"
                attempt_errors.append(msg)
                logger.exception("Download timeout: %s", msg)
                time.sleep(min(1.0, 0.3 * outer_attempt))
            except Exception as exc:
                msg = f"attempt={outer_attempt} impersonate={impersonate!r} error={exc.__class__.__name__}: {exc}"
                attempt_errors.append(msg)
                logger.exception("Download attempt failed: %s", msg)
                time.sleep(min(1.0, 0.3 * outer_attempt))

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
                "Menyiapkan metadata...",
                parse_mode="HTML",
            )

            meta, platform_from_meta, preferred_impersonate = _fetch_metadata(url, tmpdir)
            if meta:
                _safe_send_message(
                    bot,
                    chat_id,
                    _extract_metadata(meta, platform_from_meta, url),
                    parse_mode="HTML",
                )
            else:
                _safe_send_message(
                    bot,
                    chat_id,
                    "ℹ️ Metadata tidak tersedia, bot akan lanjut mengunduh.",
                    parse_mode="HTML",
                )

            selected_platform, paths = _download_with_retries(
                url,
                tmpdir,
                preferred_impersonate=preferred_impersonate,
            )

            if not paths:
                raise RuntimeError("Media tidak ditemukan dari tautan ini.")

            platform_for_send = _pretty_platform(meta or {}, url) if meta else selected_platform
            _safe_send_message(
                bot,
                chat_id,
                "✅ <b>File ditemukan</b>\n"
                f"Platform: <b>{_escape(platform_for_send)}</b>\n"
                f"Jumlah file: <b>{len(paths)}</b>\n\n"
                "Mengirim ke Telegram...",
                parse_mode="HTML",
            )

            title = ""
            if meta:
                title = str(meta.get("title") or meta.get("fulltitle") or "").strip()
            if not title and paths:
                title = _truncate(paths[0].stem, 120)
            if not title:
                title = "Media"

            sent = 0
            total = len(paths)
            for index, path in enumerate(paths, start=1):
                caption = _make_caption(title, platform_for_send, path, index, total)
                if _send_file_with_fallback(bot, chat_id, path, caption):
                    sent += 1
                else:
                    logger.error("Failed to send file after all fallbacks: %s", path)

            if sent == 0:
                raise RuntimeError("Tidak ada file yang berhasil dikirim ke Telegram.")

            summary = (
                "✅ <b>Download selesai</b>\n\n"
                + (
                    f"Berhasil mengirim <b>{sent}/{total}</b> file.\n"
                    if sent < total
                    else f"Berhasil mengirim <b>{sent}</b> file.\n"
                )
                + "Silakan kirim tautan lain."
            )

            _safe_send_message(
                bot,
                chat_id,
                summary,
                reply_markup=_downloader_keyboard(),
                parse_mode="HTML",
            )

        except Exception as exc:
            report_local_error("process_downloader_url", exc)
            _safe_send_message(
                bot,
                chat_id,
                "⚠️ <b>Gagal mengunduh media.</b>\n\n"
                f"{_escape(str(exc))}",
                reply_markup=_downloader_keyboard(),
                parse_mode="HTML",
            )


def process_downloader_message(bot, message) -> bool:
    if not getattr(message, "text", None):
        return False
    if not allowed(message.from_user.id):
        return False
    if message.from_user.id not in DOWNLOADER_STATE:
        return False
    if message.text.startswith("/"):
        return False

    url = _normalize_url(message.text or "")
    if not url:
        _safe_send_message(
            bot,
            message.chat.id,
            "Silakan kirim tautan yang valid.",
            reply_markup=_downloader_keyboard(),
            parse_mode="HTML",
        )
        return True

    process_downloader_url(bot, message.chat.id, url)
    return True


def process_downloader_callback(bot, call) -> bool:
    data = getattr(call, "data", "") or ""
    user_id = call.from_user.id

    if data in ("main:utilitas", "utilitas"):
        clear_pending(user_id)
        show_utilitas_home(bot, call.message.chat.id)
        _safe_answer_callback(bot, call.id)
        return True

    if data == "util:download":
        clear_pending(user_id)
        DOWNLOADER_STATE[user_id] = True
        show_downloader_home(bot, call.message.chat.id)
        _safe_answer_callback(bot, call.id)
        return True

    if data == "util:back":
        clear_pending(user_id)
        show_utilitas_home(bot, call.message.chat.id)
        _safe_answer_callback(bot, call.id)
        return True

    if data == "exit_mode":
        clear_pending(user_id)
        show_utilitas_home(bot, call.message.chat.id)
        _safe_answer_callback(bot, call.id)
        return True

    if data == "back_dashboard":
        clear_pending(user_id)
        _safe_answer_callback(bot, call.id)
        return True

    return False


def register_download(bot):
    global REGISTERED_DOWNLOAD_HANDLERS
    if REGISTERED_DOWNLOAD_HANDLERS:
        return

    @bot.callback_query_handler(func=lambda call: getattr(call, "data", None) in {"main:utilitas", "util:download", "util:back", "exit_mode", "back_dashboard"})
    def _download_callbacks(call):
        if not allowed(call.from_user.id):
            _safe_answer_callback(bot, call.id, "Akses ditolak")
            return
        process_downloader_callback(bot, call)

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

    @bot.message_handler(
        content_types=["text"],
        func=lambda m: allowed(m.from_user.id)
        and m.from_user.id in DOWNLOADER_STATE
        and bool(getattr(m, "text", None))
        and not m.text.startswith("/"),
    )
    def downloader_text_handler(message):
        process_downloader_message(bot, message)

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

    REGISTERED_DOWNLOAD_HANDLERS = True
    logger.info("Download handlers registered")
