import html
import re
from collections import defaultdict
from datetime import datetime
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID
from database import supabase

WALLET_TABLE = "finance_wallets"
TX_TABLE = "finance_transactions"

_pending = {}


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def fmt_idr(value: int) -> str:
    return f"Rp{int(value):,}".replace(",", ".")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def parse_amount(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    if not digits:
        raise ValueError("Nominal harus angka")
    amount = int(digits)
    if amount <= 0:
        raise ValueError("Nominal harus lebih dari 0")
    return amount


def safe_edit(bot, chat_id: int, message_id: int, text: str, markup=None):
    try:
        bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


def main_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Keuangan", callback_data="main:finance"))
    return kb


def finance_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Tambah", callback_data="finance:add"),
        InlineKeyboardButton("➖ Kurang", callback_data="finance:sub"),
    )
    kb.add(
        InlineKeyboardButton("📊 Grafik", callback_data="finance:graph"),
        InlineKeyboardButton("📄 Laporan", callback_data="finance:report"),
    )
    kb.add(
        InlineKeyboardButton("🕒 Transaksi Terakhir", callback_data="finance:recent"),
        InlineKeyboardButton("🗑 Hapus Transaksi", callback_data="finance:delete"),
    )
    kb.add(InlineKeyboardButton("🔙 Kembali", callback_data="main:menu"))
    return kb


def back_finance_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Kembali", callback_data="finance:menu"))
    return kb


def after_success_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Tambah Lagi", callback_data="finance:add"),
        InlineKeyboardButton("➖ Kurang Lagi", callback_data="finance:sub"),
    )
    kb.add(
        InlineKeyboardButton("💵 Menu Keuangan", callback_data="finance:menu"),
        InlineKeyboardButton("🏠 Menu Utama", callback_data="main:menu"),
    )
    return kb


def delete_list_keyboard(txs):
    kb = InlineKeyboardMarkup(row_width=1)
    for tx in txs[:8]:
        label = f"🗑 {tx['tx_type']} | {fmt_idr(tx['amount'])} | {tx['id'][:8]}"
        kb.add(InlineKeyboardButton(label, callback_data=f"finance:delask:{tx['id']}"))
    kb.add(InlineKeyboardButton("🔙 Kembali", callback_data="finance:menu"))
    return kb


def confirm_delete_keyboard(tx_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Ya, hapus", callback_data=f"finance:delok:{tx_id}"),
        InlineKeyboardButton("❌ Batal", callback_data="finance:delete"),
    )
    return kb


def get_wallet(user_id: int):
    res = (
        supabase.table(WALLET_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if rows:
        return rows[0]

    created = (
        supabase.table(WALLET_TABLE)
        .insert({"user_id": user_id, "balance": 0, "updated_at": now_iso()})
        .execute()
    )
    return created.data[0]


def set_wallet_balance(user_id: int, balance: int):
    supabase.table(WALLET_TABLE).update(
        {"balance": int(balance), "updated_at": now_iso()}
    ).eq("user_id", user_id).execute()


def insert_transaction(user_id: int, tx_type: str, amount: int, note: str, before: int, after: int):
    supabase.table(TX_TABLE).insert({
        "user_id": user_id,
        "tx_type": tx_type,
        "amount": int(amount),
        "note": note or None,
        "balance_before": int(before),
        "balance_after": int(after),
        "created_at": now_iso(),
    }).execute()


def list_transactions(user_id: int, limit: int = 10, desc: bool = True):
    res = (
        supabase.table(TX_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=desc)
        .limit(limit)
        .execute()
    )
    return res.data or []


def all_transactions(user_id: int):
    res = (
        supabase.table(TX_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


def recalc_balance(user_id: int) -> int:
    txs = all_transactions(user_id)
    balance = 0
    for tx in txs:
        amount = int(tx["amount"])
        if tx["tx_type"] == "plus":
            balance += amount
        else:
            balance -= amount
    set_wallet_balance(user_id, balance)
    return balance


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()


def home_text(user_id: int) -> str:
    wallet = get_wallet(user_id)
    return (
        "💵 <b>Catat Keuangan</b>\n\n"
        f"Saldo saat ini: <b>{fmt_idr(wallet['balance'])}</b>\n\n"
        "Pilih menu yang ingin digunakan."
    )


def show_home(bot, chat_id: int, message_id: int, user_id: int):
    safe_edit(bot, chat_id, message_id, home_text(user_id), finance_keyboard())


def set_pending(user_id: int, action: str, chat_id: int, message_id: int):
    _pending[user_id] = {
        "action": action,
        "chat_id": chat_id,
        "message_id": message_id,
    }


def clear_pending(user_id: int):
    _pending.pop(user_id, None)


def graph_bytes(user_id: int):
    txs = all_transactions(user_id)
    if not txs:
        return None

    day_map = defaultdict(lambda: {"plus": 0, "minus": 0})
    for tx in txs:
        day = parse_dt(tx["created_at"]).date().isoformat()
        day_map[day][tx["tx_type"]] += int(tx["amount"])

    days = sorted(day_map.keys())
    income = [day_map[d]["plus"] for d in days]
    expense = [day_map[d]["minus"] for d in days]

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(days, income, marker="o", label="Pemasukan")
    ax.plot(days, expense, marker="o", label="Pengeluaran")
    ax.set_title("Grafik Keuangan")
    ax.set_xlabel("Tanggal")
    ax.set_ylabel("Nominal")
    ax.legend()
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "grafik_keuangan.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def report_bytes(user_id: int):
    wallet = get_wallet(user_id)
    txs = all_transactions(user_id)
    plus_total = sum(int(x["amount"]) for x in txs if x["tx_type"] == "plus")
    minus_total = sum(int(x["amount"]) for x in txs if x["tx_type"] == "minus")
    recent = txs[-8:][::-1]

    fig, ax = plt.subplots(figsize=(12, 8))
    ax.axis("off")

    lines = [
        "LAPORAN KEUANGAN",
        "",
        f"Saldo Saat Ini : {fmt_idr(wallet['balance'])}",
        f"Total Tambah   : {fmt_idr(plus_total)}",
        f"Total Kurang   : {fmt_idr(minus_total)}",
        f"Jumlah Transaksi: {len(txs)}",
        "",
        "Transaksi Terakhir:",
    ]

    for i, tx in enumerate(recent, start=1):
        dt = parse_dt(tx["created_at"]).strftime("%d-%m-%Y %H:%M")
        note = tx.get("note") or "-"
        lines.append(f"{i}. {dt} | {tx['tx_type']} | {fmt_idr(tx['amount'])} | {note}")

    fig.text(0.03, 0.97, "\n".join(lines), va="top", fontsize=12, family="monospace")
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "laporan_keuangan.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def delete_recent_text(user_id: int):
    txs = list_transactions(user_id, limit=8, desc=True)
    if not txs:
        return "Belum ada transaksi untuk dihapus.", finance_keyboard()

    text_lines = ["🗑 <b>Hapus Transaksi</b>", "", "Pilih transaksi yang mau dihapus."]
    for tx in txs:
        dt = parse_dt(tx["created_at"]).strftime("%d-%m-%Y %H:%M")
        note = tx.get("note") or "-"
        text_lines.append(f"• {dt} | {tx['tx_type']} | {fmt_idr(tx['amount'])} | {note}")

    return "\n".join(text_lines), delete_list_keyboard(txs)


def register_finance(bot):
    @bot.callback_query_handler(func=lambda call: call.data == "main:finance" or call.data.startswith("finance:"))
    def finance_router(call):
        user_id = call.from_user.id
        if not allowed(user_id):
            bot.answer_callback_query(call.id, "Akses ditolak")
            return

        data = call.data
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        if data == "main:finance" or data == "finance:menu":
            clear_pending(user_id)
            show_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "finance:add":
            set_pending(user_id, "add_amount", chat_id, message_id)
            safe_edit(
                bot,
                chat_id,
                message_id,
                "➕ <b>Tambah Saldo</b>\n\nKirim nominal yang ingin ditambahkan.\nContoh: <code>50000</code>",
                back_finance_keyboard(),
            )
            bot.answer_callback_query(call.id)
            return

        if data == "finance:sub":
            set_pending(user_id, "sub_amount", chat_id, message_id)
            safe_edit(
                bot,
                chat_id,
                message_id,
                "➖ <b>Kurangi Saldo</b>\n\nKirim nominal yang ingin dikurangi.\nContoh: <code>25000</code>",
                back_finance_keyboard(),
            )
            bot.answer_callback_query(call.id)
            return

        if data == "finance:graph":
            bot.answer_callback_query(call.id, "Membuat grafik...")

            bio = graph_bytes(user_id)

            bot.send_photo(
               chat_id,
               photo=bio,
               caption="📊 Grafik Keuangan"
            )
            show_home(
               bot,
               chat_id,
               message_id,
               user_id
            )
            return
            safe_edit(bot, chat_id, message_id, "📊 Grafik sedang dikirim...", finance_keyboard())
            bot.send_photo(chat_id, photo=bio, caption="📊 Grafik Keuangan")
            return

       if data == "finance:report":
            bot.answer_callback_query(call.id, "Membuat laporan...")
            bio = report_bytes(user_id)

            bot.send_photo(
                chat_id,
                photo=bio,
                caption="📄 Laporan Keuangan"
            )
            show_home(
                bot,
                chat_id,
                message_id,
                user_id
            )
            return
        if data == "finance:recent":
            bot.answer_callback_query(call.id)
            txs = list_transactions(user_id, limit=10, desc=True)
            if not txs:
                safe_edit(bot, chat_id, message_id, "Belum ada transaksi.", finance_keyboard())
                return

            lines = ["🕒 <b>Transaksi Terakhir</b>", ""]
            for i, tx in enumerate(txs, start=1):
                dt = parse_dt(tx["created_at"]).strftime("%d-%m-%Y %H:%M")
                note = tx.get("note") or "-"
                lines.append(f"{i}. {dt}\n   {tx['tx_type']} | {fmt_idr(tx['amount'])} | {note}\n")

            safe_edit(bot, chat_id, message_id, "\n".join(lines), finance_keyboard())
            return

        if data == "finance:delete":
            bot.answer_callback_query(call.id)
            text, kb = delete_recent_text(user_id)
            safe_edit(bot, chat_id, message_id, text, kb)
            return

        if data.startswith("finance:delask:"):
            tx_id = data.split("finance:delask:", 1)[1]
            bot.answer_callback_query(call.id)
            txs = list_transactions(user_id, limit=50, desc=True)
            tx = next((x for x in txs if x["id"] == tx_id), None)
            if not tx:
                safe_edit(bot, chat_id, message_id, "Transaksi tidak ditemukan.", finance_keyboard())
                return

            dt = parse_dt(tx["created_at"]).strftime("%d-%m-%Y %H:%M")
            note = tx.get("note") or "-"
            text = (
                "Yakin mau hapus transaksi ini?\n\n"
                f"{dt}\n"
                f"{tx['tx_type']} | {fmt_idr(tx['amount'])} | {note}"
            )
            safe_edit(bot, chat_id, message_id, text, confirm_delete_keyboard(tx_id))
            return

        if data.startswith("finance:delok:"):
            tx_id = data.split("finance:delok:", 1)[1]
            bot.answer_callback_query(call.id, "Menghapus...")

            res = (
                supabase.table(TX_TABLE)
                .select("*")
                .eq("user_id", user_id)
                .eq("id", tx_id)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if not rows:
                safe_edit(bot, chat_id, message_id, "Transaksi tidak ditemukan.", finance_keyboard())
                return

            supabase.table(TX_TABLE).delete().eq("id", tx_id).eq("user_id", user_id).execute()
            new_balance = recalc_balance(user_id)

            safe_edit(
                bot,
                chat_id,
                message_id,
                "✅ Transaksi berhasil dihapus.\n\n"
                f"Saldo sekarang: <b>{fmt_idr(new_balance)}</b>",
                after_success_keyboard(),
            )
            return

    def handle_text(message):
        user_id = message.from_user.id
        if not allowed(user_id):
            return

        state = _pending.get(user_id)
        if not state:
            return

        action = state["action"]
        chat_id = state["chat_id"]
        message_id = state["message_id"]

        if action in ("add_amount", "sub_amount"):
            try:
                amount = parse_amount(message.text)
            except Exception:
                safe_edit(
                    bot,
                    chat_id,
                    message_id,
                    "❌ Nominal tidak valid.\nKirim angka saja, contoh: <code>50000</code>",
                    back_finance_keyboard(),
                )
                return

            state["amount"] = amount
            state["action"] = "add_note" if action == "add_amount" else "sub_note"
            _pending[user_id] = state

            safe_edit(
                bot,
                chat_id,
                message_id,
                f"Nominal: <b>{fmt_idr(amount)}</b>\n\nKirim keterangan transaksi.\nKetik <code>-</code> jika kosong.",
                back_finance_keyboard(),
            )
            return

        if action in ("add_note", "sub_note"):
            note = (message.text or "").strip()
            if note == "-":
                note = ""

            amount = int(state["amount"])
            tx_type = "plus" if action == "add_note" else "minus"

            wallet = get_wallet(user_id)
            before = int(wallet["balance"])
            after = before + amount if tx_type == "plus" else before - amount

            if after < 0:
                clear_pending(user_id)
                safe_edit(
                    bot,
                    chat_id,
                    message_id,
                    "❌ Saldo tidak cukup untuk transaksi ini.",
                    finance_keyboard(),
                )
                return

            set_wallet_balance(user_id, after)
            insert_transaction(user_id, tx_type, amount, note, before, after)
            clear_pending(user_id)

            label = "Tambah" if tx_type == "plus" else "Kurang"
            note_show = note if note else "-"
            safe_edit(
                bot,
                chat_id,
                message_id,
                "✅ Transaksi berhasil.\n\n"
                f"Jenis      : <b>{label}</b>\n"
                f"Nominal    : <b>{fmt_idr(amount)}</b>\n"
                f"Keterangan : <b>{html.escape(note_show)}</b>\n\n"
                f"Saldo sekarang: <b>{fmt_idr(after)}</b>",
                after_success_keyboard(),
            )
            return

    return handle_text
