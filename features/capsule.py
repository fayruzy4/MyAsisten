import html
import os
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any, Dict, List, Optional

import boto3
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from botocore.exceptions import BotoCoreError, ClientError
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID
from database import supabase

CAPSULES_TABLE = "time_capsules"
ITEMS_TABLE = "time_capsule_items"
REFLECTIONS_TABLE = "time_capsule_reflections"

PENDING: Dict[int, Dict[str, Any]] = {}

MONTHS_ID = {
    1: "Januari",
    2: "Februari",
    3: "Maret",
    4: "April",
    5: "Mei",
    6: "Juni",
    7: "Juli",
    8: "Agustus",
    9: "September",
    10: "Oktober",
    11: "November",
    12: "Desember",
}

CREATE_OPEN_CHOICES = [
    ("Besok", 1),
    ("1 Minggu", 7),
    ("1 Bulan", 30),
    ("3 Bulan", 90),
    ("6 Bulan", 180),
    ("1 Tahun", 365),
    ("3 Tahun", 1095),
]

ITEM_TYPES = [
    ("📝 Teks", "text"),
    ("📷 Foto", "photo"),
    ("🎥 Video", "video"),
    ("🎤 Voice Note", "voice"),
    ("📄 Dokumen", "document"),
    ("🔗 Link", "link"),
    ("📍 Lokasi", "location"),
]

ITEM_SHORT = {
    "text": "📝",
    "photo": "📷",
    "video": "🎥",
    "voice": "🎤",
    "document": "📄",
    "link": "🔗",
    "location": "📍",
}

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "myasisten-capsules").strip()
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "").strip()

_R2_CLIENT = None


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def today_date() -> date:
    return date.today()


def clean_text(text: Any) -> str:
    return " ".join(str(text or "").strip().split())


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def to_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def db_date(value: Any) -> Optional[date]:
    if value in (None, "", "-"):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    raw = str(value).strip()
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw[:10], pattern).date()
        except ValueError:
            continue
    return None


def date_text(value: Any) -> str:
    d = db_date(value)
    if d is None:
        return "-"
    return f"{d.day:02d} {MONTHS_ID[d.month]} {d.year}"


def days_left(open_date: Any) -> int:
    d = db_date(open_date)
    if d is None:
        return 0
    return (d - today_date()).days


def truncate_text(value: Any, max_len: int = 80) -> str:
    text = clean_text(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _r2_client():
    global _R2_CLIENT
    if _R2_CLIENT is not None:
        return _R2_CLIENT

    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY):
        raise RuntimeError(
            "R2 belum dikonfigurasi. Isi R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, dan R2_SECRET_ACCESS_KEY."
        )

    _R2_CLIENT = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )
    return _R2_CLIENT


def _r2_key(user_id: int, capsule_id: str, kind: str, filename: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename)
    return f"capsules/{user_id}/{capsule_id}/{kind}/{uuid.uuid4().hex[:10]}_{safe}"


def _upload_bytes(key: str, data: bytes, content_type: str = "application/octet-stream"):
    client = _r2_client()
    client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type or "application/octet-stream",
    )


def _download_bytes(key: str) -> bytes:
    client = _r2_client()
    obj = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
    return obj["Body"].read()


def _telegram_file_bytes(bot, file_id: str) -> bytes:
    file_info = bot.get_file(file_id)
    return bot.download_file(file_info.file_path)


def _q(table: str):
    return supabase.table(table)


def clear_pending(user_id: int):
    PENDING.pop(user_id, None)


def _set_pending(user_id: int, chat_id: int, message_id: int, mode: str, step: str, data: Optional[Dict[str, Any]] = None):
    PENDING[user_id] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "mode": mode,
        "step": step,
        "data": data or {},
    }


def _edit_or_send(bot, chat_id: int, message_id: Optional[int], text: str, markup=None):
    if message_id:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            pass

    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


def _send_photo(bot, chat_id: int, bio, caption: str, markup=None):
    bot.send_photo(chat_id, photo=bio, caption=caption, reply_markup=markup, parse_mode="HTML")


def _single_button_keyboard(label: str, callback_data: str):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(label, callback_data=callback_data))
    return kb


def _back_keyboard(back_callback: str):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("❌ Batal", callback_data=back_callback))
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def refresh_capsules(user_id: int):
    res = (
        _q(CAPSULES_TABLE)
        .select("id, open_date, status")
        .eq("user_id", user_id)
        .eq("status", "locked")
        .lte("open_date", today_date().isoformat())
        .execute()
    )
    rows = res.data or []
    for row in rows:
        _q(CAPSULES_TABLE).update(
            {
                "status": "open",
                "opened_at": now_iso(),
                "updated_at": now_iso(),
            }
        ).eq("id", row["id"]).eq("user_id", user_id).execute()


def get_capsule(user_id: int, capsule_id: str) -> Optional[Dict[str, Any]]:
    res = (
        _q(CAPSULES_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("id", capsule_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def list_capsules(user_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
    q = _q(CAPSULES_TABLE).select("*").eq("user_id", user_id).order("created_at", desc=True)
    if status:
        q = q.eq("status", status)
    res = q.execute()
    return res.data or []


def list_items(user_id: int, capsule_id: str) -> List[Dict[str, Any]]:
    res = (
        _q(ITEMS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("capsule_id", capsule_id)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


def list_reflections(user_id: int, capsule_id: str) -> List[Dict[str, Any]]:
    res = (
        _q(REFLECTIONS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("capsule_id", capsule_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def create_capsule(user_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    res = _q(CAPSULES_TABLE).insert(
        {
            "user_id": user_id,
            "name": data["name"],
            "description": data.get("description") or None,
            "open_date": data["open_date"].isoformat(),
            "status": "locked",
            "opened_at": None,
            "archived_at": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    ).execute()

    capsule = (res.data or [None])[0]
    if not capsule:
        raise RuntimeError("Gagal menyimpan kapsul.")
    return capsule


def update_capsule(user_id: int, capsule_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    capsule = get_capsule(user_id, capsule_id)
    if not capsule:
        return None

    payload = {"updated_at": now_iso()}
    payload.update(updates)

    if "name" in payload:
        payload["name"] = clean_text(payload["name"])[:80]
    if "description" in payload:
        payload["description"] = clean_text(payload["description"])[:300]
    if "open_date" in payload:
        if isinstance(payload["open_date"], date):
            payload["open_date"] = payload["open_date"].isoformat()
        else:
            d = db_date(payload["open_date"])
            if d is not None:
                payload["open_date"] = d.isoformat()

    _q(CAPSULES_TABLE).update(payload).eq("id", capsule_id).eq("user_id", user_id).execute()
    return get_capsule(user_id, capsule_id)


def delete_capsule(user_id: int, capsule_id: str) -> bool:
    capsule = get_capsule(user_id, capsule_id)
    if not capsule:
        return False

    _q(ITEMS_TABLE).delete().eq("capsule_id", capsule_id).eq("user_id", user_id).execute()
    _q(REFLECTIONS_TABLE).delete().eq("capsule_id", capsule_id).eq("user_id", user_id).execute()
    _q(CAPSULES_TABLE).delete().eq("id", capsule_id).eq("user_id", user_id).execute()
    return True


def add_item(
    user_id: int,
    capsule_id: str,
    item_type: str,
    *,
    text_content: Optional[str] = None,
    r2_key: Optional[str] = None,
    file_name: Optional[str] = None,
    mime_type: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
):
    _q(ITEMS_TABLE).insert(
        {
            "capsule_id": capsule_id,
            "user_id": user_id,
            "item_type": item_type,
            "text_content": text_content,
            "r2_key": r2_key,
            "file_name": file_name,
            "mime_type": mime_type,
            "latitude": latitude,
            "longitude": longitude,
            "created_at": now_iso(),
        }
    ).execute()


def add_reflection(user_id: int, capsule_id: str, feel: str, note: str):
    _q(REFLECTIONS_TABLE).insert(
        {
            "capsule_id": capsule_id,
            "user_id": user_id,
            "feel": feel,
            "note": note,
            "created_at": now_iso(),
        }
    ).execute()


def capsule_counts(capsule_id: str, items: List[Dict[str, Any]]) -> Dict[str, int]:
    c = Counter([item["item_type"] for item in items])
    return dict(c)


def _capsule_status_label(capsule: Dict[str, Any]) -> str:
    status = capsule.get("status", "locked")
    if status == "locked":
        return "🔒 Terkunci"
    if status == "open":
        return "🔓 Terbuka"
    return "🗂 Arsip"


def _capsule_progress_label(capsule: Dict[str, Any]) -> str:
    left = days_left(capsule.get("open_date"))
    if capsule.get("status") == "open":
        return "Sudah terbuka"
    if capsule.get("status") == "archived":
        return "Diarsipkan"
    if left <= 0:
        return "Siap dibuka"
    return f"{left} hari lagi"


def _capsule_detail_text(capsule: Dict[str, Any], items: List[Dict[str, Any]]) -> str:
    counts = capsule_counts(capsule["id"], items)
    type_lines = []
    for _, kind in ITEM_TYPES:
        if counts.get(kind, 0):
            type_lines.append(f"{ITEM_SHORT[kind]} {counts.get(kind, 0)}")
    type_summary = " | ".join(type_lines) if type_lines else "-"

    return (
        f"⏳ <b>{escape(capsule['name'])}</b>\n\n"
        f"Status\n<b>{_capsule_status_label(capsule)}</b>\n"
        f"Dibuka\n<b>{date_text(capsule.get('open_date'))}</b>\n"
        f"Sisa\n<b>{_capsule_progress_label(capsule)}</b>\n"
        f"Isi\n<b>{len(items)} item</b>\n"
        f"Jenis Isi\n<b>{type_summary}</b>\n"
        f"Deskripsi\n<b>{escape(capsule.get('description') or '-')}</b>"
    )


def _capsule_home_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("➕ Buat Kapsul", callback_data="cap:create"),
        InlineKeyboardButton("📦 Daftar Kapsul", callback_data="cap:list"),
        InlineKeyboardButton("🔓 Kapsul Terbuka", callback_data="cap:opened"),
        InlineKeyboardButton("🗂 Arsip", callback_data="cap:archive"),
        InlineKeyboardButton("📊 Statistik", callback_data="cap:stats"),
        InlineKeyboardButton("🔙 Produktivitas", callback_data="main:produktif"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _date_choice_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    for label, days in CREATE_OPEN_CHOICES:
        kb.add(InlineKeyboardButton(label, callback_data=f"cap:create:days:{days}"))
    kb.add(
        InlineKeyboardButton("📅 Pilih Sendiri", callback_data="cap:create:custom"),
        InlineKeyboardButton("❌ Batal", callback_data="cap:cancel"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
        InlineKeyboardButton("🔙 Produktivitas", callback_data="main:produktif"),
    )
    return kb


def _detail_keyboard(capsule: Dict[str, Any], items: List[Dict[str, Any]]):
    kb = InlineKeyboardMarkup(row_width=2)

    if capsule.get("status") != "archived":
        kb.add(
            InlineKeyboardButton("📝 Teks", callback_data=f"cap:item:text:{capsule['id']}"),
            InlineKeyboardButton("📷 Foto", callback_data=f"cap:item:photo:{capsule['id']}"),
        )
        kb.add(
            InlineKeyboardButton("🎥 Video", callback_data=f"cap:item:video:{capsule['id']}"),
            InlineKeyboardButton("🎤 Voice", callback_data=f"cap:item:voice:{capsule['id']}"),
        )
        kb.add(
            InlineKeyboardButton("📄 Dokumen", callback_data=f"cap:item:document:{capsule['id']}"),
            InlineKeyboardButton("🔗 Link", callback_data=f"cap:item:link:{capsule['id']}"),
        )
        kb.add(
            InlineKeyboardButton("📍 Lokasi", callback_data=f"cap:item:location:{capsule['id']}"),
        )

    if capsule.get("status") in ("locked", "open") and db_date(capsule.get("open_date")) <= today_date():
        kb.add(InlineKeyboardButton("📖 Buka Kapsul", callback_data=f"cap:open:{capsule['id']}"))

    if capsule.get("status") == "open":
        kb.add(InlineKeyboardButton("💬 Refleksi", callback_data=f"cap:reflect:{capsule['id']}"))
        kb.add(InlineKeyboardButton("🗂 Arsipkan", callback_data=f"cap:archiveok:{capsule['id']}"))

    kb.add(
        InlineKeyboardButton("✏️ Edit", callback_data=f"cap:edit:{capsule['id']}"),
        InlineKeyboardButton("🗑 Hapus", callback_data=f"cap:delask:{capsule['id']}"),
    )
    kb.add(
        InlineKeyboardButton("🔙 Daftar", callback_data="cap:list"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _edit_keyboard(capsule_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🏷 Nama", callback_data=f"cap:editf:{capsule_id}:name"),
        InlineKeyboardButton("📝 Deskripsi", callback_data=f"cap:editf:{capsule_id}:description"),
    )
    kb.add(
        InlineKeyboardButton("📅 Tanggal Buka", callback_data=f"cap:editf:{capsule_id}:open_date"),
        InlineKeyboardButton("🔙 Kembali", callback_data=f"cap:view:{capsule_id}"),
    )
    return kb


def _confirm_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Simpan", callback_data="cap:create:save"),
        InlineKeyboardButton("❌ Batal", callback_data="cap:cancel"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
        InlineKeyboardButton("🔙 Produktivitas", callback_data="main:produktif"),
    )
    return kb


def _delete_keyboard(capsule_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Ya, Hapus", callback_data=f"cap:delok:{capsule_id}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"cap:view:{capsule_id}"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
        InlineKeyboardButton("🔙 Produktivitas", callback_data="main:produktif"),
    )
    return kb


def _item_pick_keyboard(capsule_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    for label, kind in ITEM_TYPES:
        kb.add(InlineKeyboardButton(label, callback_data=f"cap:item:{kind}:{capsule_id}"))
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=f"cap:view:{capsule_id}"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _list_keyboard(capsules: List[Dict[str, Any]], filter_status: Optional[str] = None):
    kb = InlineKeyboardMarkup(row_width=1)
    for capsule in capsules:
        label = f"{_capsule_status_label(capsule)} {truncate_text(capsule['name'], 26)} • {_capsule_progress_label(capsule)}"
        kb.add(InlineKeyboardButton(label, callback_data=f"cap:view:{capsule['id']}"))
    kb.add(
        InlineKeyboardButton("➕ Buat Kapsul", callback_data="cap:create"),
        InlineKeyboardButton("🔙 Kapsul", callback_data="cap:home"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
        InlineKeyboardButton("🔙 Produktivitas", callback_data="main:produktif"),
    )
    return kb


def _stats_chart(capsules: List[Dict[str, Any]]):
    if not capsules:
        return None

    counts = Counter([c.get("status", "locked") for c in capsules])
    labels = []
    values = []
    mapping = {"locked": "Terkunci", "open": "Terbuka", "archived": "Arsip"}
    for key in ("locked", "open", "archived"):
        if counts.get(key, 0):
            labels.append(mapping[key])
            values.append(counts[key])

    if not values:
        return None

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.set_title("Status Kapsul")
    ax.axis("equal")
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "capsule_stats.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def _stats_text(user_id: int):
    refresh_capsules(user_id)
    all_capsules = list_capsules(user_id)
    locked = len([c for c in all_capsules if c.get("status") == "locked"])
    opened = len([c for c in all_capsules if c.get("status") == "open"])
    archived = len([c for c in all_capsules if c.get("status") == "archived"])
    items = _q(ITEMS_TABLE).select("item_type").eq("user_id", user_id).execute().data or []
    count_items = len(items)
    by_type = Counter([row["item_type"] for row in items])

    type_lines = []
    for label, kind in ITEM_TYPES:
        if by_type.get(kind, 0):
            type_lines.append(f"{label}: {by_type[kind]}")
    type_summary = "\n".join(type_lines) if type_lines else "-"

    return (
        "📊 <b>Statistik Kapsul</b>\n\n"
        f"Total Kapsul\n<b>{len(all_capsules)}</b>\n"
        f"Masih Terkunci\n<b>{locked}</b>\n"
        f"Sudah Terbuka\n<b>{opened}</b>\n"
        f"Diarsipkan\n<b>{archived}</b>\n"
        f"Total Isi\n<b>{count_items}</b>\n\n"
        f"Jenis File\n<b>{escape(type_summary)}</b>"
    )


def _caption_for_item(item: Dict[str, Any]) -> str:
    kind = item["item_type"]
    prefix = {
        "text": "📝 Teks",
        "photo": "📷 Foto",
        "video": "🎥 Video",
        "voice": "🎤 Voice Note",
        "document": "📄 Dokumen",
        "link": "🔗 Link",
        "location": "📍 Lokasi",
    }.get(kind, "📦 Item")
    note = item.get("text_content") or ""
    if note:
        return f"{prefix}\n\n{escape(note)}"
    return prefix


def _send_capsule_item(bot, chat_id: int, item: Dict[str, Any]):
    kind = item["item_type"]

    if kind == "text":
        bot.send_message(chat_id, f"📝 <b>Pesan</b>\n\n{escape(item.get('text_content') or '-')}", parse_mode="HTML")
        return

    if kind == "link":
        bot.send_message(chat_id, f"🔗 <b>Link</b>\n\n{escape(item.get('text_content') or '-')}", parse_mode="HTML")
        return

    if kind == "location":
        lat = float(item.get("latitude"))
        lon = float(item.get("longitude"))
        bot.send_location(chat_id, latitude=lat, longitude=lon)
        return

    if not item.get("r2_key"):
        bot.send_message(chat_id, f"⚠️ File {kind} tidak ditemukan.", parse_mode="HTML")
        return

    try:
        data = _download_bytes(item["r2_key"])
    except (ClientError, BotoCoreError, RuntimeError):
        bot.send_message(chat_id, "⚠️ File tidak bisa diambil dari R2.", parse_mode="HTML")
        return

    bio = BytesIO(data)
    bio.name = item.get("file_name") or f"{kind}.bin"
    caption = _caption_for_item(item)

    if kind == "photo":
        bot.send_photo(chat_id, photo=bio, caption=caption, parse_mode="HTML")
    elif kind == "video":
        bot.send_video(chat_id, video=bio, caption=caption, parse_mode="HTML")
    elif kind == "voice":
        bot.send_voice(chat_id, voice=bio, caption=caption, parse_mode="HTML")
    else:
        bot.send_document(chat_id, document=bio, caption=caption, parse_mode="HTML")


def _send_capsule_contents(bot, chat_id: int, user_id: int, capsule_id: str):
    capsule = get_capsule(user_id, capsule_id)
    if not capsule:
        bot.send_message(chat_id, "Kapsul tidak ditemukan.", parse_mode="HTML")
        return

    items = list_items(user_id, capsule_id)
    if not items:
        bot.send_message(chat_id, "Belum ada isi di kapsul ini.", parse_mode="HTML")
        return

    for item in items:
        _send_capsule_item(bot, chat_id, item)


def _summary_text(data: Dict[str, Any]) -> str:
    return (
        "⏳ <b>Ringkasan Kapsul</b>\n\n"
        f"Nama\n<b>{escape(data['name'])}</b>\n"
        f"Deskripsi\n<b>{escape(data.get('description') or '-')}</b>\n"
        f"Dibuka\n<b>{date_text(data['open_date'])}</b>\n"
        f"Status\n<b>🔒 Terkunci</b>"
    )


def _home_text():
    return (
        "⏳ <b>Kapsul Waktu</b>\n\n"
        "Simpan pesan, media, dan dokumen untuk dibuka nanti."
    )


def _list_text(title: str, capsules: List[Dict[str, Any]]) -> str:
    if not capsules:
        return f"{title}\n\nBelum ada kapsul."
    lines = [f"{title}", ""]
    for c in capsules:
        lines.append(
            f"• <b>{escape(c['name'])}</b>\n"
            f"  {_capsule_status_label(c)}\n"
            f"  Dibuka: <b>{date_text(c.get('open_date'))}</b>\n"
            f"  Sisa: <b>{_capsule_progress_label(c)}</b>\n"
        )
    return "\n".join(lines)


def show_capsule_home(bot, chat_id: int, message_id: Optional[int] = None, user_id: Optional[int] = None):
    if user_id is not None:
        refresh_capsules(user_id)
    _edit_or_send(bot, chat_id, message_id, _home_text(), _capsule_home_keyboard())


def show_capsule_list(bot, chat_id: int, message_id: Optional[int], user_id: int, status: Optional[str] = None):
    refresh_capsules(user_id)
    capsules = list_capsules(user_id, status=status)
    title = "📦 <b>Daftar Kapsul</b>" if status is None else (
        "🔓 <b>Kapsul Terbuka</b>" if status == "open" else "🗂 <b>Arsip Kapsul</b>"
    )
    _edit_or_send(bot, chat_id, message_id, _list_text(title, capsules), _list_keyboard(capsules, status))


def show_capsule_stats(bot, chat_id: int, message_id: Optional[int], user_id: int):
    refresh_capsules(user_id)
    text = _stats_text(user_id)
    chart = _stats_chart(list_capsules(user_id))
    if chart is None:
        _edit_or_send(bot, chat_id, message_id, text, _capsule_home_keyboard())
        return

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📦 Daftar", callback_data="cap:list"),
        InlineKeyboardButton("🔙 Kapsul", callback_data="cap:home"),
    )
    kb.add(
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
        InlineKeyboardButton("🔙 Produktivitas", callback_data="main:produktif"),
    )
    _send_photo(bot, chat_id, chart, text, kb)


def _show_capsule_detail(bot, chat_id: int, message_id: Optional[int], user_id: int, capsule_id: str):
    refresh_capsules(user_id)
    capsule = get_capsule(user_id, capsule_id)
    if not capsule:
        _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
        return

    items = list_items(user_id, capsule_id)
    _edit_or_send(bot, chat_id, message_id, _capsule_detail_text(capsule, items), _detail_keyboard(capsule, items))


def _show_capsule_edit_menu(bot, chat_id: int, message_id: Optional[int], user_id: int, capsule_id: str):
    capsule = get_capsule(user_id, capsule_id)
    if not capsule:
        _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
        return

    text = (
        f"✏️ <b>Edit Kapsul</b>\n\n"
        f"<b>{escape(capsule['name'])}</b>\n\n"
        "Pilih bagian yang ingin diubah."
    )
    _edit_or_send(bot, chat_id, message_id, text, _edit_keyboard(capsule_id))


def _item_prompt(kind: str) -> str:
    if kind == "text":
        return "📝 Kirim teks untuk disimpan di kapsul."
    if kind == "link":
        return "🔗 Kirim link untuk disimpan di kapsul."
    if kind == "location":
        return "📍 Kirim lokasi Telegram."
    if kind == "photo":
        return "📷 Kirim foto."
    if kind == "video":
        return "🎥 Kirim video."
    if kind == "voice":
        return "🎤 Kirim voice note."
    if kind == "document":
        return "📄 Kirim dokumen."
    return "Kirim isi kapsul."


def register_capsule(bot):
    @bot.message_handler(commands=["capsule", "kapsul", "timecapsule"])
    def open_capsule_command(message):
        if not allowed(message.from_user.id):
            return
        clear_pending(message.from_user.id)
        show_capsule_home(bot, message.chat.id, None, message.from_user.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("cap:"))
    def capsule_router(call):
        user_id = call.from_user.id
        if not allowed(user_id):
            bot.answer_callback_query(call.id, "Akses ditolak")
            return

        data = call.data
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        if data == "cap:home":
            clear_pending(user_id)
            show_capsule_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "cap:create":
            clear_pending(user_id)
            _set_pending(user_id, chat_id, message_id, "create", "name", {})
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                "➕ <b>Buat Kapsul Baru</b>\n\nKirim nama kapsul.",
                _confirm_keyboard(),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("cap:create:days:"):
            state = PENDING.get(user_id)
            if not state or state.get("mode") != "create":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return
            days = int(data.split(":")[-1])
            state["data"]["open_date"] = today_date() + timedelta(days=days)
            state["step"] = "confirm"
            PENDING[user_id] = state
            _edit_or_send(bot, chat_id, message_id, _summary_text(state["data"]), _confirm_keyboard())
            bot.answer_callback_query(call.id)
            return

        if data == "cap:create:custom":
            state = PENDING.get(user_id)
            if not state or state.get("mode") != "create":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return
            state["step"] = "open_date_custom"
            PENDING[user_id] = state
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                "📅 Kirim tanggal buka kapsul.\nFormat: dd-mm-yyyy",
                _confirm_keyboard(),
            )
            bot.answer_callback_query(call.id)
            return

        if data == "cap:create:save":
            state = PENDING.get(user_id)
            if not state or state.get("mode") != "create" or state.get("step") != "confirm":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return

            capsule = create_capsule(user_id, state["data"])
            clear_pending(user_id)
            _show_capsule_detail(bot, chat_id, message_id, user_id, capsule["id"])
            bot.answer_callback_query(call.id, "Tersimpan")
            return

        if data == "cap:cancel":
            clear_pending(user_id)
            show_capsule_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id, "Dibatalkan")
            return

        if data == "cap:list":
            clear_pending(user_id)
            show_capsule_list(bot, chat_id, message_id, user_id, status=None)
            bot.answer_callback_query(call.id)
            return

        if data == "cap:opened":
            clear_pending(user_id)
            show_capsule_list(bot, chat_id, message_id, user_id, status="open")
            bot.answer_callback_query(call.id)
            return

        if data == "cap:archive":
            clear_pending(user_id)
            show_capsule_list(bot, chat_id, message_id, user_id, status="archived")
            bot.answer_callback_query(call.id)
            return

        if data == "cap:stats":
            clear_pending(user_id)
            show_capsule_stats(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("cap:view:"):
            clear_pending(user_id)
            capsule_id = data.split(":", 2)[2]
            _show_capsule_detail(bot, chat_id, message_id, user_id, capsule_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("cap:item:"):
            clear_pending(user_id)
            parts = data.split(":")
            if len(parts) != 4:
                bot.answer_callback_query(call.id, "Data tidak valid")
                return
            _, _, kind, capsule_id = parts
            capsule = get_capsule(user_id, capsule_id)
            if not capsule:
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            _set_pending(user_id, chat_id, message_id, "item_add", kind, {"capsule_id": capsule_id, "kind": kind})
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                _item_prompt(kind),
                _back_keyboard(f"cap:view:{capsule_id}"),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("cap:open:"):
            clear_pending(user_id)
            capsule_id = data.split(":", 2)[2]
            capsule = get_capsule(user_id, capsule_id)
            if not capsule:
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            refresh_capsules(user_id)
            capsule = get_capsule(user_id, capsule_id)
            if capsule.get("status") == "locked" and db_date(capsule.get("open_date")) > today_date():
                bot.answer_callback_query(call.id, "Kapsul masih terkunci")
                _show_capsule_detail(bot, chat_id, message_id, user_id, capsule_id)
                return

            if capsule.get("status") == "locked":
                update_capsule(user_id, capsule_id, {"status": "open", "opened_at": now_iso()})
                capsule = get_capsule(user_id, capsule_id)

            _send_capsule_contents(bot, chat_id, user_id, capsule_id)
            _show_capsule_detail(bot, chat_id, message_id, user_id, capsule_id)
            bot.answer_callback_query(call.id, "Kapsul dibuka")
            return

        if data.startswith("cap:archiveok:"):
            clear_pending(user_id)
            capsule_id = data.split(":", 2)[2]
            capsule = get_capsule(user_id, capsule_id)
            if not capsule:
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            update_capsule(user_id, capsule_id, {"status": "archived", "archived_at": now_iso()})
            _show_capsule_detail(bot, chat_id, message_id, user_id, capsule_id)
            bot.answer_callback_query(call.id, "Diarsipkan")
            return

        if data.startswith("cap:delask:"):
            clear_pending(user_id)
            capsule_id = data.split(":", 2)[2]
            capsule = get_capsule(user_id, capsule_id)
            if not capsule:
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            text = (
                "⚠️ <b>Hapus Kapsul</b>\n\n"
                f"Yakin ingin menghapus <b>{escape(capsule['name'])}</b>?"
            )
            _edit_or_send(bot, chat_id, message_id, text, _delete_keyboard(capsule_id))
            bot.answer_callback_query(call.id)
            return

        if data.startswith("cap:delok:"):
            clear_pending(user_id)
            capsule_id = data.split(":", 2)[2]
            ok = delete_capsule(user_id, capsule_id)
            if not ok:
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            _edit_or_send(bot, chat_id, message_id, "✅ Kapsul berhasil dihapus.", _capsule_home_keyboard())
            bot.answer_callback_query(call.id, "Dihapus")
            return

        if data.startswith("cap:edit:"):
            clear_pending(user_id)
            capsule_id = data.split(":", 2)[2]
            capsule = get_capsule(user_id, capsule_id)
            if not capsule:
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            _show_capsule_edit_menu(bot, chat_id, message_id, user_id, capsule_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("cap:editf:"):
            clear_pending(user_id)
            parts = data.split(":")
            if len(parts) != 4:
                bot.answer_callback_query(call.id, "Data tidak valid")
                return
            _, _, capsule_id, field = parts
            capsule = get_capsule(user_id, capsule_id)
            if not capsule:
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            _set_pending(user_id, chat_id, message_id, "edit", field, {"capsule_id": capsule_id})
            prompts = {
                "name": "Kirim nama baru.",
                "description": "Kirim deskripsi baru.\nKetik - untuk kosong.",
                "open_date": "Kirim tanggal buka baru.\nFormat: dd-mm-yyyy",
            }
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                f"✏️ <b>Edit Kapsul</b>\n\n{prompts.get(field, 'Kirim nilai baru.')}",
                _back_keyboard(f"cap:view:{capsule_id}"),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("cap:reflect:"):
            clear_pending(user_id)
            capsule_id = data.split(":", 2)[2]
            capsule = get_capsule(user_id, capsule_id)
            if not capsule:
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            _set_pending(user_id, chat_id, message_id, "reflect", "feel", {"capsule_id": capsule_id})
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                "💬 <b>Refleksi Kapsul</b>\n\nTuliskan satu kata perasaanmu dulu.",
                _back_keyboard(f"cap:view:{capsule_id}"),
            )
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)

    @bot.message_handler(content_types=["photo", "video", "voice", "document", "location"])
    def capsule_media_handler(message):
        user_id = message.from_user.id
        if not allowed(user_id):
            return

        state = PENDING.get(user_id)
        if not state or state.get("mode") != "item_add":
            return

        chat_id = state["chat_id"]
        message_id = state["message_id"]
        kind = state["step"]
        data = state["data"]
        capsule_id = data.get("capsule_id")
        capsule = get_capsule(user_id, capsule_id) if capsule_id else None

        if not capsule:
            clear_pending(user_id)
            _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
            return

        try:
            if kind == "location":
                if getattr(message, "content_type", None) != "location" or not getattr(message, "location", None):
                    _edit_or_send(bot, chat_id, message_id, "Kirim lokasi Telegram.", _back_keyboard(f"cap:view:{capsule_id}"))
                    return

                loc = message.location
                add_item(
                    user_id,
                    capsule_id,
                    kind,
                    latitude=float(loc.latitude),
                    longitude=float(loc.longitude),
                )

            else:
                content_type = getattr(message, "content_type", None)
                file_id = None
                filename = None
                mime_type = None
                caption = clean_text(getattr(message, "caption", "") or "")

                if kind == "photo":
                    if content_type != "photo" or not getattr(message, "photo", None):
                        _edit_or_send(bot, chat_id, message_id, "Kirim foto.", _back_keyboard(f"cap:view:{capsule_id}"))
                        return
                    file_id = message.photo[-1].file_id
                    filename = f"photo_{uuid.uuid4().hex[:8]}.jpg"
                    mime_type = "image/jpeg"

                elif kind == "video":
                    if content_type != "video" or not getattr(message, "video", None):
                        _edit_or_send(bot, chat_id, message_id, "Kirim video.", _back_keyboard(f"cap:view:{capsule_id}"))
                        return
                    file_id = message.video.file_id
                    filename = message.video.file_name or f"video_{uuid.uuid4().hex[:8]}.mp4"
                    mime_type = getattr(message.video, "mime_type", None) or "video/mp4"

                elif kind == "voice":
                    if content_type != "voice" or not getattr(message, "voice", None):
                        _edit_or_send(bot, chat_id, message_id, "Kirim voice note.", _back_keyboard(f"cap:view:{capsule_id}"))
                        return
                    file_id = message.voice.file_id
                    filename = f"voice_{uuid.uuid4().hex[:8]}.ogg"
                    mime_type = "audio/ogg"

                elif kind == "document":
                    if content_type != "document" or not getattr(message, "document", None):
                        _edit_or_send(bot, chat_id, message_id, "Kirim dokumen.", _back_keyboard(f"cap:view:{capsule_id}"))
                        return
                    file_id = message.document.file_id
                    filename = message.document.file_name or f"document_{uuid.uuid4().hex[:8]}.bin"
                    mime_type = getattr(message.document, "mime_type", None) or "application/octet-stream"

                else:
                    _edit_or_send(bot, chat_id, message_id, "Jenis isi ini tidak didukung.", _back_keyboard(f"cap:view:{capsule_id}"))
                    return

                raw = _telegram_file_bytes(bot, file_id)
                key = _r2_key(user_id, capsule_id, kind, filename)
                _upload_bytes(key, raw, mime_type or "application/octet-stream")
                add_item(
                    user_id,
                    capsule_id,
                    kind,
                    text_content=caption or None,
                    r2_key=key,
                    file_name=filename,
                    mime_type=mime_type,
                )

        except Exception:
            _edit_or_send(bot, chat_id, message_id, "Gagal menyimpan isi kapsul.", _back_keyboard(f"cap:view:{capsule_id}"))
            return

        clear_pending(user_id)
        _show_capsule_detail(bot, chat_id, message_id, user_id, capsule_id)

    def handle_text(message):
        user_id = message.from_user.id
        if not allowed(user_id):
            return

        state = PENDING.get(user_id)
        if not state:
            return

        chat_id = state["chat_id"]
        message_id = state["message_id"]
        mode = state["mode"]
        step = state["step"]
        data = state["data"]
        text = clean_text(message.text)

        if mode == "create":
            if step == "name":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Nama kapsul tidak boleh kosong.", _confirm_keyboard())
                    return
                data["name"] = text[:80]
                state["step"] = "description"
                PENDING[user_id] = state
                _edit_or_send(bot, chat_id, message_id, "📝 Kirim deskripsi kapsul.", _confirm_keyboard())
                return

            if step == "description":
                data["description"] = "" if text in ("", "-") else text[:300]
                state["step"] = "open_date"
                PENDING[user_id] = state
                _edit_or_send(bot, chat_id, message_id, "📅 Pilih kapan kapsul dibuka.", _date_choice_keyboard())
                return

            if step == "open_date_custom":
                try:
                    d = parse_input_date(text, allow_blank=False, default_today=False)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", _date_choice_keyboard())
                    return
                data["open_date"] = d
                state["step"] = "confirm"
                PENDING[user_id] = state
                _edit_or_send(bot, chat_id, message_id, _summary_text(data), _confirm_keyboard())
                return

            return

        if mode == "edit":
            capsule_id = data.get("capsule_id")
            capsule = get_capsule(user_id, capsule_id) if capsule_id else None
            if not capsule:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                return

            updates: Dict[str, Any] = {}
            if step == "name":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Nama tidak boleh kosong.", _edit_keyboard(capsule_id))
                    return
                updates["name"] = text[:80]
            elif step == "description":
                updates["description"] = "" if text in ("", "-") else text[:300]
            elif step == "open_date":
                try:
                    updates["open_date"] = parse_input_date(text, allow_blank=False, default_today=False)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", _edit_keyboard(capsule_id))
                    return
            else:
                _edit_or_send(bot, chat_id, message_id, "Field edit tidak dikenali.", _edit_keyboard(capsule_id))
                return

            update_capsule(user_id, capsule_id, updates)
            clear_pending(user_id)
            _show_capsule_detail(bot, chat_id, message_id, user_id, capsule_id)
            return

        if mode == "item_add":
            capsule_id = data.get("capsule_id")
            kind = data.get("kind")
            capsule = get_capsule(user_id, capsule_id) if capsule_id else None
            if not capsule:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                return

            try:
                if kind in ("text", "link"):
                    if not text:
                        _edit_or_send(bot, chat_id, message_id, "Isi tidak boleh kosong.", _item_pick_keyboard(capsule_id))
                        return
                    add_item(user_id, capsule_id, kind, text_content=text[:4000])
                elif kind == "location":
                    _edit_or_send(bot, chat_id, message_id, "Kirim lokasi Telegram.", _item_pick_keyboard(capsule_id))
                    return
                else:
                    _edit_or_send(bot, chat_id, message_id, "Kirim file sesuai jenisnya.", _item_pick_keyboard(capsule_id))
                    return
            except Exception:
                _edit_or_send(bot, chat_id, message_id, "Gagal menyimpan isi kapsul.", _item_pick_keyboard(capsule_id))
                return

            clear_pending(user_id)
            _show_capsule_detail(bot, chat_id, message_id, user_id, capsule_id)
            return

        if mode == "reflect":
            capsule_id = data.get("capsule_id")
            capsule = get_capsule(user_id, capsule_id) if capsule_id else None
            if not capsule:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Kapsul tidak ditemukan.", _capsule_home_keyboard())
                return

            if step == "feel":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Isi perasaan tidak boleh kosong.", _back_keyboard(f"cap:view:{capsule_id}"))
                    return
                data["feel"] = text[:80]
                state["step"] = "note"
                PENDING[user_id] = state
                _edit_or_send(bot, chat_id, message_id, "📝 Tuliskan refleksi singkatmu.", _back_keyboard(f"cap:view:{capsule_id}"))
                return

            if step == "note":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Refleksi tidak boleh kosong.", _back_keyboard(f"cap:view:{capsule_id}"))
                    return
                add_reflection(user_id, capsule_id, data.get("feel") or "-", text[:2000])
                clear_pending(user_id)
                _show_capsule_detail(bot, chat_id, message_id, user_id, capsule_id)
                return

        return

    return handle_text
