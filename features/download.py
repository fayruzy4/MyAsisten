import mimetypes
import os
import re
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID

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


def show_utilitas_home(bot, chat_id: int):
    bot.send_message(
        chat_id,
        "🛠️ <b>Utilitas</b>\n\nPilih alat yang ingin digunakan.",
        reply_markup=_utilitas_keyboard(),
        parse_mode="HTML",
    )


def show_downloader_home(bot, chat_id: int):
    bot.send_message(
        chat_id,
        "📥 <b>Universal Downloader</b>\n\n"
        "Kirim tautan dari platform apa pun yang didukung.\n"
        "Bot akan mendeteksi platform secara otomatis.",
        reply_markup=_downloader_keyboard(),
        parse_mode="HTML",
    )


def _gather_download_paths(ydl, info: dict, tmpdir: str) -> list[Path]:
    paths: list[Path] = []

    requested = info.get("requested_downloads") or []
    for item in requested:
        if not isinstance(item, dict):
            continue
        for key in ("filepath", "filename", "_filename"):
            fp = item.get(key)
            if fp and os.path.exists(fp):
                p = Path(fp)
                if p not in paths:
                    paths.append(p)
                break

    for key in ("filepath", "_filename"):
        fp = info.get(key)
        if fp and os.path.exists(fp):
            p = Path(fp)
            if p not in paths:
                paths.append(p)

    if not paths:
        try:
            fp = ydl.prepare_filename(info)
            if fp and os.path.exists(fp):
                paths.append(Path(fp))
        except Exception:
            pass

    if not paths:
        for p in sorted(Path(tmpdir).rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() in {".part", ".json", ".ytdl", ".tmp"}:
                continue
            if p not in paths:
                paths.append(p)

    unique: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)

    return unique


def _download_with_ytdlp(url: str, tmpdir: str):
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise RuntimeError("Paket yt-dlp belum terpasang di VPS.") from exc

    ydl_opts = {
        "format": "bv*+ba/best",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(tmpdir, "%(title).200s-%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "socket_timeout": 20,
        "retries": 3,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
        paths = _gather_download_paths(ydl, info, tmpdir)
        return info, paths


def _send_local_path(bot, chat_id: int, path: Path, platform: str, title: str) -> int:
    if not path.exists():
        return 0

    mime, _ = mimetypes.guess_type(path.name)
    ext = path.suffix.lower()
    caption = f"📥 {_escape(title[:80])}\n{_escape(platform)}"

    with open(path, "rb") as fh:
        try:
            if ext == ".gif":
                bot.send_animation(chat_id, animation=fh, caption=caption, parse_mode="HTML")
                return 1
            if mime and mime.startswith("image/"):
                bot.send_photo(chat_id, photo=fh, caption=caption, parse_mode="HTML")
                return 1
            if mime and mime.startswith("video/"):
                bot.send_video(chat_id, video=fh, caption=caption, parse_mode="HTML")
                return 1
            if mime and mime.startswith("audio/"):
                bot.send_audio(chat_id, audio=fh, caption=caption, parse_mode="HTML")
                return 1

            bot.send_document(chat_id, document=fh, caption=caption, parse_mode="HTML")
            return 1
        except Exception:
            fh.seek(0)
            try:
                bot.send_document(chat_id, document=fh, caption=caption, parse_mode="HTML")
                return 1
            except Exception:
                traceback.print_exc()
                return 0


def process_downloader_url(bot, chat_id: int, url: str):
    tmpdir = tempfile.mkdtemp(prefix="universal_downloader_")
    try:
        bot.send_message(chat_id, "⏳ Sedang memproses tautan...", parse_mode="HTML")

        info, paths = _download_with_ytdlp(url, tmpdir)
        platform = _pretty_platform(info, url)
        title = str(info.get("title") or info.get("playlist_title") or "Media").strip()

        if not paths:
            raise RuntimeError("Media tidak ditemukan dari tautan ini.")

        bot.send_message(
            chat_id,
            f"✅ Platform terdeteksi: <b>{_escape(platform)}</b>\n"
            f"File ditemukan: <b>{len(paths)}</b>\n\n"
            f"Mengirim hasil...",
            parse_mode="HTML",
        )

        sent = 0
        for path in paths:
            sent += _send_local_path(bot, chat_id, path, platform, title)

        if sent == 0:
            raise RuntimeError("Tidak ada file yang berhasil dikirim ke Telegram.")

        bot.send_message(
            chat_id,
            f"✅ Download selesai.\n\n"
            f"Berhasil mengirim <b>{sent}</b> file.\n"
            f"Silakan kirim tautan lain.",
            reply_markup=_downloader_keyboard(),
            parse_mode="HTML",
        )
    except Exception as e:
        traceback.print_exc()
        bot.send_message(
            chat_id,
            f"⚠️ Gagal mengunduh media.\n\n{_escape(str(e))}",
            reply_markup=_downloader_keyboard(),
            parse_mode="HTML",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def register_download(bot):
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
    @bot.callback_query_handler(func=lambda call: call.data == "main:utilitas")
    def open_utilitas(call):
        if not allowed(call.from_user.id):
            bot.answer_callback_query(call.id, "Akses ditolak")
            return

        clear_pending(call.from_user.id)
        show_utilitas_home(bot, call.message.chat.id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "util:download")
    def open_downloader(call):
        if not allowed(call.from_user.id):
            bot.answer_callback_query(call.id, "Akses ditolak")
            return
        clear_pending(call.from_user.id)
        DOWNLOADER_STATE[call.from_user.id] = True
        show_downloader_home(bot, call.message.chat.id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "util:back")
    def back_to_utilitas(call):
        if not allowed(call.from_user.id):
            bot.answer_callback_query(call.id, "Akses ditolak")
            return
        clear_pending(call.from_user.id)
        DOWNLOADER_STATE.pop(call.from_user.id, None)
        show_utilitas_home(bot, call.message.chat.id)
        bot.answer_callback_query(call.id)

    @bot.message_handler(
        content_types=["text"],
        func=lambda m: allowed(m.from_user.id) and m.from_user.id in DOWNLOADER_STATE and not m.text.startswith("/")
    )
    def downloader_text_handler(message):
        url = _normalize_url(message.text or "")
        if not url:
            bot.send_message(
                message.chat.id,
                "Silakan kirim tautan yang valid.",
                reply_markup=_downloader_keyboard(),
                parse_mode="HTML",
            )
            return

        process_downloader_url(bot, message.chat.id, url)

    @bot.message_handler(
        content_types=["photo", "video", "document", "audio", "voice", "sticker", "animation", "location", "contact"]
    )
    def downloader_non_text(message):
        if not allowed(message.from_user.id):
            return
        if message.from_user.id not in DOWNLOADER_STATE:
            return

        bot.send_message(
            message.chat.id,
            "Silakan kirim tautan sebagai teks.",
            reply_markup=_downloader_keyboard(),
            parse_mode="HTML",
    )
