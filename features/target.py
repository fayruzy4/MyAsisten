import html
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from config import OWNER_ID
from database import supabase

GOALS_TABLE = "target_goals"
ENTRIES_TABLE = "target_entries"

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


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def to_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def money(value: Any) -> str:
    return f"Rp{to_int(value):,}".replace(",", ".")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def clean_text(text: Any) -> str:
    return " ".join(str(text or "").strip().split())


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def truncate_text(value: Any, max_len: int = 80) -> str:
    text = clean_text(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def parse_amount(text: str) -> int:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if not digits:
        raise ValueError("Nominal harus angka")
    amount = int(digits)
    if amount < 0:
        raise ValueError("Nominal tidak boleh negatif")
    return amount


def parse_optional_amount(text: str) -> int:
    raw = clean_text(text)
    if raw in ("", "-"):
        return 0
    return parse_amount(raw)


def parse_input_date(text: str, allow_blank: bool = True, default_today: bool = False) -> Optional[date]:
    raw = clean_text(text)
    if raw in ("", "-"):
        if default_today:
            return date.today()
        return None if allow_blank else date.today()

    for pattern in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            pass

    raise ValueError("Format tanggal tidak valid")


def date_text(value: Any) -> str:
    if value in (None, "", "-"):
        return "-"
    if isinstance(value, date) and not isinstance(value, datetime):
        d = value
    elif isinstance(value, datetime):
        d = value.date()
    else:
        d = parse_input_date(str(value), allow_blank=True, default_today=False)
    if d is None:
        return "-"
    return f"{d.day:02d} {MONTHS_ID[d.month]} {d.year}"


def progress_pct(current: int, goal: int) -> int:
    if goal <= 0:
        return 0
    return int(round((current / goal) * 100))


def status_text(current: int, goal: int) -> str:
    return "🟢 TERCAPAI" if current >= goal else "🟡 AKTIF"


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


def _edit_or_send_photo(bot, chat_id: int, message_id: Optional[int], photo, caption: str, markup=None):
    if message_id:
        try:
            bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
                reply_markup=markup,
            )
            return
        except Exception:
            pass

    bot.send_photo(chat_id, photo=photo, caption=caption, reply_markup=markup, parse_mode="HTML")


def _q(table: str):
    return supabase.table(table)


def _get_goal(user_id: int, goal_id: str) -> Optional[Dict[str, Any]]:
    res = (
        _q(GOALS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("id", goal_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def _list_goals(user_id: int) -> List[Dict[str, Any]]:
    res = (
        _q(GOALS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def _list_entries(user_id: int, goal_id: str, desc: bool = True) -> List[Dict[str, Any]]:
    res = (
        _q(ENTRIES_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("goal_id", goal_id)
        .order("created_at", desc=desc)
        .execute()
    )
    return res.data or []


def _recalc_goal(user_id: int, goal_id: str) -> Optional[Dict[str, Any]]:
    goal = _get_goal(user_id, goal_id)
    if not goal:
        return None

    entries = _list_entries(user_id, goal_id, desc=False)
    current = 0
    for entry in entries:
        amount = to_int(entry["amount"])
        if entry["tx_type"] == "add":
            current += amount
        else:
            current -= amount
        if current < 0:
            current = 0

    goal_amount = to_int(goal["goal_amount"])
    status = "done" if current >= goal_amount else "active"

    _q(GOALS_TABLE).update(
        {
            "current_amount": current,
            "status": status,
            "updated_at": now_iso(),
        }
    ).eq("id", goal_id).eq("user_id", user_id).execute()

    goal["current_amount"] = current
    goal["status"] = status
    return goal


def _insert_goal(user_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    res = _q(GOALS_TABLE).insert(
        {
            "user_id": user_id,
            "name": data["name"],
            "goal_amount": to_int(data["goal_amount"]),
            "current_amount": to_int(data.get("current_amount", 0)),
            "target_date": data["target_date"].isoformat() if data.get("target_date") else None,
            "note": data.get("note") or None,
            "status": "active",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    ).execute()

    goal = (res.data or [None])[0]
    if not goal:
        raise RuntimeError("Gagal menyimpan target")

    initial = to_int(data.get("current_amount", 0))
    if initial > 0:
        _q(ENTRIES_TABLE).insert(
            {
                "goal_id": goal["id"],
                "user_id": user_id,
                "tx_type": "add",
                "amount": initial,
                "note": "Saldo awal",
                "created_at": now_iso(),
            }
        ).execute()

    return _recalc_goal(user_id, goal["id"]) or goal


def _record_entry(user_id: int, goal_id: str, tx_type: str, amount: int, note: str = "") -> Optional[Dict[str, Any]]:
    goal = _get_goal(user_id, goal_id)
    if not goal:
        return None

    amount = to_int(amount)
    if amount <= 0:
        return goal

    if tx_type == "remove":
        current = to_int(goal.get("current_amount"))
        if current <= 0:
            return goal
        amount = min(amount, current)

    _q(ENTRIES_TABLE).insert(
        {
            "goal_id": goal_id,
            "user_id": user_id,
            "tx_type": tx_type,
            "amount": amount,
            "note": note or None,
            "created_at": now_iso(),
        }
    ).execute()

    return _recalc_goal(user_id, goal_id)


def _update_goal(user_id: int, goal_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    goal = _get_goal(user_id, goal_id)
    if not goal:
        return None

    payload = {"updated_at": now_iso()}
    payload.update(updates)

    if "name" in payload:
        payload["name"] = clean_text(payload["name"])[:80]
    if "goal_amount" in payload:
        payload["goal_amount"] = to_int(payload["goal_amount"])
    if "current_amount" in payload:
        payload["current_amount"] = max(to_int(payload["current_amount"]), 0)
    if "target_date" in payload:
        if payload["target_date"] in (None, "", "-"):
            payload["target_date"] = None
        elif isinstance(payload["target_date"], date):
            payload["target_date"] = payload["target_date"].isoformat()
        else:
            d = parse_input_date(str(payload["target_date"]), allow_blank=True, default_today=False)
            payload["target_date"] = d.isoformat() if d else None
    if "note" in payload:
        payload["note"] = clean_text(payload["note"])[:200]

    _q(GOALS_TABLE).update(payload).eq("id", goal_id).eq("user_id", user_id).execute()
    return _recalc_goal(user_id, goal_id)


def _delete_goal(user_id: int, goal_id: str) -> bool:
    goal = _get_goal(user_id, goal_id)
    if not goal:
        return False

    _q(ENTRIES_TABLE).delete().eq("goal_id", goal_id).eq("user_id", user_id).execute()
    _q(GOALS_TABLE).delete().eq("id", goal_id).eq("user_id", user_id).execute()
    return True


def _goal_remaining(goal: Dict[str, Any]) -> int:
    return max(to_int(goal["goal_amount"]) - to_int(goal["current_amount"]), 0)


def _goal_progress(goal: Dict[str, Any]) -> int:
    goal_amount = to_int(goal["goal_amount"])
    current = to_int(goal["current_amount"])
    if goal_amount <= 0:
        return 0
    pct = progress_pct(current, goal_amount)
    return min(pct, 100)


def _goal_detail_caption(goal: Dict[str, Any]) -> str:
    goal_amount = to_int(goal["goal_amount"])
    current = to_int(goal["current_amount"])
    remaining = _goal_remaining(goal)
    pct = _goal_progress(goal)
    note = truncate_text(goal.get("note") or "-", 90)
    status = status_text(current, goal_amount)

    progress_line = f"{pct}%"
    if current >= goal_amount:
        progress_line = f"{pct}% (Tercapai)"

    return (
        f"🎯 <b>{escape(goal['name'])}</b>\n\n"
        f"Status\n<b>{status}</b>\n\n"
        f"Target\n<b>{money(goal_amount)}</b>\n"
        f"Terkumpul\n<b>{money(current)}</b>\n"
        f"Sisa\n<b>{money(remaining)}</b>\n"
        f"Progress\n<b>{progress_line}</b>\n"
        f"Tanggal Target\n<b>{date_text(goal.get('target_date'))}</b>\n"
        f"Catatan\n<b>{escape(note)}</b>"
    )


def _goal_summary_text(goal: Dict[str, Any]) -> str:
    goal_amount = to_int(goal["goal_amount"])
    current = to_int(goal["current_amount"])
    remaining = _goal_remaining(goal)
    pct = _goal_progress(goal)
    status = status_text(current, goal_amount)

    progress_line = f"{pct}%"
    if current >= goal_amount:
        progress_line = f"{pct}% (Tercapai)"

    return (
        "🎯 <b>Ringkasan Target</b>\n\n"
        f"Nama\n<b>{escape(goal['name'])}</b>\n"
        f"Target\n<b>{money(goal_amount)}</b>\n"
        f"Terkumpul\n<b>{money(current)}</b>\n"
        f"Sisa\n<b>{money(remaining)}</b>\n"
        f"Progress\n<b>{progress_line}</b>\n"
        f"Status\n<b>{status}</b>\n"
        f"Tanggal Target\n<b>{date_text(goal.get('target_date'))}</b>\n"
        f"Catatan\n<b>{escape(truncate_text(goal.get('note') or '-', 90))}</b>"
    )


def _goal_stats_text(user_id: int) -> Tuple[str, Optional[date]]:
    goals = _list_goals(user_id)
    total_goal = 0
    total_current = 0
    total_remaining = 0
    active = 0
    done = 0
    nearest: Optional[date] = None

    for goal in goals:
        goal = _recalc_goal(user_id, goal["id"]) or goal
        goal_amount = to_int(goal["goal_amount"])
        current = to_int(goal["current_amount"])
        remaining = _goal_remaining(goal)
        total_goal += goal_amount
        total_current += current
        total_remaining += remaining

        if current >= goal_amount:
            done += 1
        else:
            active += 1
            due = None
            if goal.get("target_date"):
                due = parse_input_date(str(goal["target_date"]), allow_blank=True, default_today=False)
            if due and (nearest is None or due < nearest):
                nearest = due

    text = (
        "📊 <b>Statistik Target</b>\n\n"
        f"Total Target\n<b>{money(total_goal)}</b>\n"
        f"Terkumpul\n<b>{money(total_current)}</b>\n"
        f"Sisa\n<b>{money(total_remaining)}</b>\n"
        f"Target Aktif\n<b>{active}</b>\n"
        f"Target Tercapai\n<b>{done}</b>\n"
        f"Jatuh Tempo Terdekat\n<b>{date_text(nearest)}</b>"
    )
    return text, nearest


def _goal_progress_chart(goal: Dict[str, Any]):
    goal_amount = to_int(goal["goal_amount"])
    current = to_int(goal["current_amount"])
    remaining = _goal_remaining(goal)

    if goal_amount <= 0:
        return None

    if current <= 0 and remaining <= 0:
        remaining = goal_amount

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.pie(
        [current, remaining],
        labels=["Terkumpul", "Sisa Target"],
        autopct="%1.1f%%",
        startangle=90,
    )
    ax.set_title("Progress Target")
    ax.axis("equal")
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "target_progress.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def _overview_chart(user_id: int):
    goals = _list_goals(user_id)
    total_goal = 0
    total_current = 0
    total_remaining = 0

    for goal in goals:
        goal = _recalc_goal(user_id, goal["id"]) or goal
        total_goal += to_int(goal["goal_amount"])
        total_current += to_int(goal["current_amount"])
        total_remaining += _goal_remaining(goal)

    if total_goal <= 0:
        return None

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.pie(
        [total_current, total_remaining],
        labels=["Terkumpul", "Sisa Target"],
        autopct="%1.1f%%",
        startangle=90,
    )
    ax.set_title("Statistik Target Keseluruhan")
    ax.axis("equal")
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "target_overview.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def _build_home_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("➕ Buat Target Baru", callback_data="tgt:create"),
        InlineKeyboardButton("📋 Daftar Target", callback_data="tgt:list"),
        InlineKeyboardButton("📊 Statistik Target", callback_data="tgt:stats"),
        InlineKeyboardButton("🔙 Keuangan", callback_data="main:keuangan"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _build_list_keyboard(goals: List[Dict[str, Any]]):
    kb = InlineKeyboardMarkup(row_width=1)
    for goal in goals:
        goal = _recalc_goal(goal["user_id"], goal["id"]) or goal
        goal_amount = to_int(goal["goal_amount"])
        current = to_int(goal["current_amount"])
        remaining = _goal_remaining(goal)
        pct = _goal_progress(goal)
        icon = "✅" if current >= goal_amount else "🎯"
        label = f"{icon} {truncate_text(goal['name'], 24)} • {pct}% • {money(current)}/{money(goal_amount)}"
        if current >= goal_amount:
            label = f"✅ {truncate_text(goal['name'], 24)} • {money(current)}/{money(goal_amount)}"
        else:
            label = f"🎯 {truncate_text(goal['name'], 24)} • {money(current)}/{money(goal_amount)}"
        kb.add(InlineKeyboardButton(label, callback_data=f"tgt:view:{goal['id']}"))

    kb.add(
        InlineKeyboardButton("🔙 Target", callback_data="tgt:home"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _build_detail_keyboard(goal_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Tambah Tabungan", callback_data=f"tgt:addask:{goal_id}"),
        InlineKeyboardButton("➖ Tarik Dana", callback_data=f"tgt:subask:{goal_id}"),
    )
    kb.add(
        InlineKeyboardButton("📜 Riwayat", callback_data=f"tgt:hist:{goal_id}"),
        InlineKeyboardButton("✏️ Edit Target", callback_data=f"tgt:edit:{goal_id}"),
    )
    kb.add(
        InlineKeyboardButton("🗑 Hapus Target", callback_data=f"tgt:delask:{goal_id}"),
        InlineKeyboardButton("🔙 Daftar Target", callback_data="tgt:list"),
    )
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def _build_edit_keyboard(goal_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🏷 Nama", callback_data=f"tgt:editf:{goal_id}:name"),
        InlineKeyboardButton("💰 Nominal Target", callback_data=f"tgt:editf:{goal_id}:goal_amount"),
    )
    kb.add(
        InlineKeyboardButton("📅 Tanggal Target", callback_data=f"tgt:editf:{goal_id}:target_date"),
        InlineKeyboardButton("📝 Catatan", callback_data=f"tgt:editf:{goal_id}:note"),
    )
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=f"tgt:view:{goal_id}"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    return kb


def _build_confirm_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Simpan", callback_data="tgt:save"),
        InlineKeyboardButton("❌ Batal", callback_data="tgt:cancel"),
    )
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def _build_delete_keyboard(goal_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Ya, Hapus", callback_data=f"tgt:delok:{goal_id}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"tgt:view:{goal_id}"),
    )
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def _build_amount_cancel_keyboard(back_callback: str):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("❌ Batal", callback_data=back_callback))
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def _show_goal_detail(bot, chat_id: int, message_id: Optional[int], user_id: int, goal_id: str):
    goal = _recalc_goal(user_id, goal_id)
    if not goal:
        _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
        return

    chart = _goal_progress_chart(goal)
    caption = _goal_detail_caption(goal)
    kb = _build_detail_keyboard(goal_id)

    if chart is None:
        _edit_or_send(bot, chat_id, message_id, caption, kb)
        return

    _edit_or_send_photo(bot, chat_id, message_id, chart, caption, kb)


def _show_goal_history(bot, chat_id: int, message_id: Optional[int], user_id: int, goal_id: str):
    goal = _get_goal(user_id, goal_id)
    if not goal:
        _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
        return

    entries = _list_entries(user_id, goal_id, desc=True)
    lines = [f"📜 <b>Riwayat - {escape(goal['name'])}</b>", ""]
    if not entries:
        lines.append("Belum ada riwayat transaksi.")
    else:
        for i, entry in enumerate(entries, start=1):
            dt = datetime.fromisoformat(str(entry["created_at"]).replace("Z", "+00:00"))
            kind = "➕ Tambah" if entry["tx_type"] == "add" else "➖ Tarik"
            lines.append(
                f"{i}. {dt.strftime('%d-%m-%Y %H:%M')} | {kind} | {money(entry['amount'])} | {escape(entry.get('note') or '-')}"
            )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=f"tgt:view:{goal_id}"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"),
    )
    _edit_or_send(bot, chat_id, message_id, "\n".join(lines), kb)


def show_target_home(bot, chat_id: int, message_id: Optional[int] = None, user_id: Optional[int] = None):
    text = (
        "🎯 <b>Target Tabungan</b>\n\n"
        "Kelola target tabungan pribadi dengan progress, riwayat, dan pie chart."
    )
    _edit_or_send(bot, chat_id, message_id, text, _build_home_keyboard())


def _show_goal_list(bot, chat_id: int, message_id: Optional[int], user_id: int):
    goals = _list_goals(user_id)
    if not goals:
        text = (
            "🎯 <b>Daftar Target</b>\n\n"
            "Belum ada target yang dibuat."
        )
        _edit_or_send(bot, chat_id, message_id, text, _build_home_keyboard())
        return

    lines = ["🎯 <b>Daftar Target</b>", ""]
    for goal in goals:
        goal = _recalc_goal(user_id, goal["id"]) or goal
        goal_amount = to_int(goal["goal_amount"])
        current = to_int(goal["current_amount"])
        remaining = _goal_remaining(goal)
        pct = _goal_progress(goal)
        status = "✅ Tercapai" if current >= goal_amount else "🟡 Aktif"
        lines.append(
            f"• <b>{escape(goal['name'])}</b>\n"
            f"  Terkumpul: <b>{money(current)}</b>\n"
            f"  Sisa: <b>{money(remaining)}</b>\n"
            f"  Progress: <b>{pct}%</b>\n"
            f"  Status: <b>{status}</b>\n"
        )

    _edit_or_send(bot, chat_id, message_id, "\n".join(lines), _build_list_keyboard(goals))


def _show_goal_stats(bot, chat_id: int, message_id: Optional[int], user_id: int):
    text, _ = _goal_stats_text(user_id)
    chart = _overview_chart(user_id)

    if chart is None:
        _edit_or_send(bot, chat_id, message_id, text, _build_home_keyboard())
        return

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📋 Daftar Target", callback_data="tgt:list"),
        InlineKeyboardButton("🔙 Target", callback_data="tgt:home"),
    )
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    _edit_or_send_photo(bot, chat_id, message_id, chart, text, kb)


def _create_summary_text(data: Dict[str, Any]) -> str:
    goal_amount = to_int(data["goal_amount"])
    current = to_int(data.get("current_amount", 0))
    remaining = max(goal_amount - current, 0)
    pct = progress_pct(current, goal_amount)
    note = truncate_text(data.get("note") or "-", 90)
    target_date = data.get("target_date")
    status = "Tercapai" if current >= goal_amount else "Aktif"

    return (
        "🎯 <b>Ringkasan Target</b>\n\n"
        f"Nama\n<b>{escape(data['name'])}</b>\n"
        f"Target\n<b>{money(goal_amount)}</b>\n"
        f"Terkumpul Awal\n<b>{money(current)}</b>\n"
        f"Sisa\n<b>{money(remaining)}</b>\n"
        f"Progress\n<b>{min(pct, 100)}%</b>\n"
        f"Status\n<b>{status}</b>\n"
        f"Tanggal Target\n<b>{date_text(target_date)}</b>\n"
        f"Catatan\n<b>{escape(note)}</b>"
    )


def _goal_edit_prompt(field: str) -> str:
    if field == "name":
        return "🏷 Kirim nama target baru."
    if field == "goal_amount":
        return "💰 Kirim nominal target baru."
    if field == "target_date":
        return "📅 Kirim tanggal target baru.\nFormat: dd-mm-yyyy\nKosong = tidak ada."
    if field == "note":
        return "📝 Kirim catatan baru.\nKosong = -"
    return "Kirim nilai baru."


def _amount_prompt(flow: str) -> str:
    if flow == "add":
        return (
            "➕ <b>Tambah Tabungan</b>\n\n"
            "Kirim nominal yang ingin ditambahkan ke target."
        )
    return (
        "➖ <b>Tarik Dana</b>\n\n"
        "Kirim nominal yang ingin dikurangi dari target."
    )


def _note_prompt(flow: str, amount: int, name: str) -> str:
    icon = "➕" if flow == "add" else "➖"
    label = "Tambah" if flow == "add" else "Tarik"
    return (
        f"{icon} <b>{label} Target</b>\n\n"
        f"Target: <b>{escape(name)}</b>\n"
        f"Nominal: <b>{money(amount)}</b>\n\n"
        "Kirim catatan.\n"
        "Ketik <code>-</code> jika kosong."
    )


def _apply_and_show_goal(bot, chat_id: int, message_id: Optional[int], user_id: int, goal_id: str):
    _show_goal_detail(bot, chat_id, message_id, user_id, goal_id)


def register_target(bot):
    @bot.message_handler(commands=["target", "tabungan"])
    def open_target_command(message):
        if not allowed(message.from_user.id):
            return
        clear_pending(message.from_user.id)
        show_target_home(bot, message.chat.id, None, message.from_user.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("tgt:"))
    def target_router(call):
        user_id = call.from_user.id
        if not allowed(user_id):
            bot.answer_callback_query(call.id, "Akses ditolak")
            return

        data = call.data
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        if data == "tgt:home":
            clear_pending(user_id)
            show_target_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "tgt:create":
            clear_pending(user_id)
            PENDING[user_id] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "mode": "create",
                "step": "name",
                "data": {},
            }
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                "🎯 <b>Buat Target Baru</b>\n\nKirim nama target.",
                _build_amount_cancel_keyboard("tgt:home"),
            )
            bot.answer_callback_query(call.id)
            return

        if data == "tgt:list":
            clear_pending(user_id)
            _show_goal_list(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "tgt:stats":
            clear_pending(user_id)
            _show_goal_stats(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("tgt:view:"):
            clear_pending(user_id)
            goal_id = data.split(":", 2)[2]
            _show_goal_detail(bot, chat_id, message_id, user_id, goal_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("tgt:hist:"):
            clear_pending(user_id)
            goal_id = data.split(":", 2)[2]
            _show_goal_history(bot, chat_id, message_id, user_id, goal_id)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("tgt:addask:"):
            clear_pending(user_id)
            goal_id = data.split(":", 2)[2]
            goal = _get_goal(user_id, goal_id)
            if not goal:
                _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            PENDING[user_id] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "mode": "add",
                "step": "amount",
                "data": {"goal_id": goal_id},
            }
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                _amount_prompt("add"),
                _build_amount_cancel_keyboard(f"tgt:view:{goal_id}"),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("tgt:subask:"):
            clear_pending(user_id)
            goal_id = data.split(":", 2)[2]
            goal = _get_goal(user_id, goal_id)
            if not goal:
                _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            PENDING[user_id] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "mode": "remove",
                "step": "amount",
                "data": {"goal_id": goal_id},
            }
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                _amount_prompt("remove"),
                _build_amount_cancel_keyboard(f"tgt:view:{goal_id}"),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("tgt:edit:"):
            clear_pending(user_id)
            goal_id = data.split(":", 2)[2]
            goal = _get_goal(user_id, goal_id)
            if not goal:
                _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            _edit_or_send(
                bot,
                chat_id,
                message_id,
                f"✏️ <b>Edit Target</b>\n\nPilih data yang ingin diubah.\n\n<b>{escape(goal['name'])}</b>",
                _build_edit_keyboard(goal_id),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("tgt:editf:"):
            clear_pending(user_id)
            parts = data.split(":")
            if len(parts) != 4:
                bot.answer_callback_query(call.id, "Data tidak valid")
                return

            _, _, goal_id, field = parts[0], parts[1], parts[2], parts[3]
            goal = _get_goal(user_id, goal_id)
            if not goal:
                _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            PENDING[user_id] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "mode": "edit",
                "step": field,
                "data": {"goal_id": goal_id},
            }
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                f"✏️ <b>Edit Target</b>\n\n{_goal_edit_prompt(field)}",
                _build_amount_cancel_keyboard(f"tgt:view:{goal_id}"),
            )
            bot.answer_callback_query(call.id)
            return

        if data.startswith("tgt:delask:"):
            clear_pending(user_id)
            goal_id = data.split(":", 2)[2]
            goal = _get_goal(user_id, goal_id)
            if not goal:
                _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            text = (
                "⚠️ <b>Hapus Target</b>\n\n"
                f"Yakin ingin menghapus <b>{escape(goal['name'])}</b>?\n"
                f"Target: <b>{money(goal['goal_amount'])}</b>\n"
                f"Terkumpul: <b>{money(goal['current_amount'])}</b>"
            )
            _edit_or_send(bot, chat_id, message_id, text, _build_delete_keyboard(goal_id))
            bot.answer_callback_query(call.id)
            return

        if data.startswith("tgt:delok:"):
            goal_id = data.split(":", 2)[2]
            ok = _delete_goal(user_id, goal_id)
            clear_pending(user_id)
            if not ok:
                _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return
            _edit_or_send(bot, chat_id, message_id, "✅ Target berhasil dihapus.", _build_home_keyboard())
            bot.answer_callback_query(call.id, "Dihapus")
            return

        if data == "tgt:save":
            state = PENDING.get(user_id)
            if not state or state.get("mode") != "create" or state.get("step") != "confirm":
                bot.answer_callback_query(call.id, "Langkah sudah lewat")
                return

            goal = _insert_goal(user_id, state["data"])
            clear_pending(user_id)
            _show_goal_detail(bot, chat_id, message_id, user_id, goal["id"])
            bot.answer_callback_query(call.id, "Tersimpan")
            return

        if data == "tgt:cancel":
            clear_pending(user_id)
            show_target_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id, "Dibatalkan")
            return

        bot.answer_callback_query(call.id)

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
                    _edit_or_send(bot, chat_id, message_id, "Nama target tidak boleh kosong.", _build_amount_cancel_keyboard("tgt:home"))
                    return
                data["name"] = text[:80]
                state["step"] = "goal_amount"
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "💰 Kirim nominal target.",
                    _build_amount_cancel_keyboard("tgt:home"),
                )
                return

            if step == "goal_amount":
                try:
                    amount = parse_amount(text)
                    if amount <= 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Nominal target harus angka dan lebih dari 0.", _build_amount_cancel_keyboard("tgt:home"))
                    return
                data["goal_amount"] = amount
                state["step"] = "current_amount"
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "💵 Terkumpul awal?\nKosong = 0",
                    _build_amount_cancel_keyboard("tgt:home"),
                )
                return

            if step == "current_amount":
                try:
                    amount = parse_optional_amount(text)
                    if amount < 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Saldo awal harus angka.", _build_amount_cancel_keyboard("tgt:home"))
                    return
                data["current_amount"] = amount
                state["step"] = "target_date"
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "📅 Tanggal target?\nFormat: dd-mm-yyyy\nKosong = tidak ada.",
                    _build_amount_cancel_keyboard("tgt:home"),
                )
                return

            if step == "target_date":
                try:
                    data["target_date"] = parse_input_date(text, allow_blank=True, default_today=False)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", _build_amount_cancel_keyboard("tgt:home"))
                    return
                state["step"] = "note"
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "📝 Catatan?\nKosong = -",
                    _build_amount_cancel_keyboard("tgt:home"),
                )
                return

            if step == "note":
                data["note"] = "" if text in ("", "-") else text[:200]
                data["target_date"] = data.get("target_date")
                state["step"] = "confirm"
                PENDING[user_id] = state
                _edit_or_send(bot, chat_id, message_id, _create_summary_text(data), _build_confirm_keyboard())
                return

        if mode in ("add", "remove"):
            goal_id = data.get("goal_id")
            goal = _get_goal(user_id, goal_id) if goal_id else None
            if not goal:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
                return

            if step == "amount":
                try:
                    amount = parse_amount(text)
                    if amount <= 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Nominal harus angka dan lebih dari 0.", _build_amount_cancel_keyboard(f"tgt:view:{goal_id}"))
                    return
                data["amount"] = amount
                state["step"] = "note"
                PENDING[user_id] = state
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    _note_prompt(mode, amount, goal["name"]),
                    _build_amount_cancel_keyboard(f"tgt:view:{goal_id}"),
                )
                return

            if step == "note":
                note = "" if text in ("", "-") else text[:200]
                amount = to_int(data.get("amount"))
                updated = _record_entry(user_id, goal_id, "add" if mode == "add" else "remove", amount, note)
                clear_pending(user_id)
                if not updated:
                    _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
                    return
                _apply_and_show_goal(bot, chat_id, message_id, user_id, goal_id)
                return

        if mode == "edit":
            goal_id = data.get("goal_id")
            goal = _get_goal(user_id, goal_id) if goal_id else None
            if not goal:
                clear_pending(user_id)
                _edit_or_send(bot, chat_id, message_id, "Target tidak ditemukan.", _build_home_keyboard())
                return

            field = step
            updates: Dict[str, Any] = {}

            if field == "name":
                if not text:
                    _edit_or_send(bot, chat_id, message_id, "Nama target tidak boleh kosong.", _build_amount_cancel_keyboard(f"tgt:view:{goal_id}"))
                    return
                updates["name"] = text[:80]

            elif field == "goal_amount":
                try:
                    amount = parse_amount(text)
                    if amount <= 0:
                        raise ValueError
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Nominal target harus angka dan lebih dari 0.", _build_amount_cancel_keyboard(f"tgt:view:{goal_id}"))
                    return
                updates["goal_amount"] = amount

            elif field == "target_date":
                try:
                    updates["target_date"] = parse_input_date(text, allow_blank=True, default_today=False)
                except Exception:
                    _edit_or_send(bot, chat_id, message_id, "Format tanggal tidak valid.", _build_amount_cancel_keyboard(f"tgt:view:{goal_id}"))
                    return

            elif field == "note":
                updates["note"] = "" if text in ("", "-") else text[:200]

            else:
                _edit_or_send(bot, chat_id, message_id, "Field edit tidak dikenali.", _build_amount_cancel_keyboard(f"tgt:view:{goal_id}"))
                return

            updated = _update_goal(user_id, goal_id, updates)
            clear_pending(user_id)
            if not updated:
                _edit_or_send(bot, chat_id, message_id, "Gagal menyimpan perubahan.", _build_edit_keyboard(goal_id))
                return
            _apply_and_show_goal(bot, chat_id, message_id, user_id, goal_id)
            return

    return handle_text


def clear_pending(user_id: int):
    PENDING.pop(user_id, None)
