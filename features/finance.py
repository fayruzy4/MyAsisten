import html
from collections import defaultdict
from datetime import datetime, timedelta
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID, BOT_TOKEN
from database import supabase

WALLET_TABLE = "finance_wallets"
TX_TABLE = "finance_transactions"

PENDING = {}

PLUS_CATEGORIES = [
    ("Gaji", "gaji"),
    ("Bonus", "bonus"),
    ("Usaha", "usaha"),
    ("Jual Barang", "jual_barang"),
    ("Lainnya", "custom"),
]

MINUS_CATEGORIES = [
    ("Makan", "makan"),
    ("Transport", "transport"),
    ("Tagihan", "tagihan"),
    ("Belanja", "belanja"),
    ("Pulsa", "pulsa"),
    ("Sedekah", "sedekah"),
    ("Kesehatan", "kesehatan"),
    ("Lainnya", "custom"),
]


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def fmt_idr(value: int) -> str:
    return f"Rp{int(value):,}".replace(",", ".")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def parse_amount(text: str) -> int:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if not digits:
        raise ValueError("Nominal harus angka")
    amount = int(digits)
    if amount <= 0:
        raise ValueError("Nominal harus lebih dari 0")
    return amount


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()


def clean_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def month_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1, tzinfo=dt.tzinfo)


def shift_month(dt: datetime, offset: int) -> datetime:
    month_index = (dt.month - 1) + offset
    year = dt.year + (month_index // 12)
    month = (month_index % 12) + 1
    return datetime(year, month, 1, tzinfo=dt.tzinfo)


def escape(value: str) -> str:
    return html.escape(value or "-")


def photo_keyboard(back_callback: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data=back_callback),
        InlineKeyboardButton("🏠 Menu Utama", callback_data="main:menu"),
    )
    return kb


def cancel_keyboard():
    return photo_keyboard("fin:home")


def finance_home_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Tambah", callback_data="fin:add"),
        InlineKeyboardButton("➖ Kurang", callback_data="fin:sub"),
    )
    kb.add(
        InlineKeyboardButton("📊 Grafik", callback_data="fin:graph_menu"),
        InlineKeyboardButton("📄 Laporan", callback_data="fin:report"),
    )
    kb.add(
        InlineKeyboardButton("🕒 Terakhir", callback_data="fin:recent"),
        InlineKeyboardButton("🗑 Hapus", callback_data="fin:delete"),
    )
    kb.add(
        InlineKeyboardButton("📈 Akumulasi", callback_data="fin:graph_accum"),
        InlineKeyboardButton("🏠 Menu Utama", callback_data="main:menu"),
    )
    return kb


def graph_menu_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📈 Tren 30 Hari", callback_data="fin:graph_trend"),
        InlineKeyboardButton("📊 Per Bulan", callback_data="fin:graph_monthly"),
    )
    kb.add(
        InlineKeyboardButton("📈 Akumulasi", callback_data="fin:graph_accum"),
        InlineKeyboardButton("🥧 Kategori", callback_data="fin:graph_category"),
    )
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data="fin:home"),
        InlineKeyboardButton("🏠 Menu Utama", callback_data="main:menu"),
    )
    return kb


def category_keyboard(flow: str):
    items = PLUS_CATEGORIES if flow == "plus" else MINUS_CATEGORIES
    kb = InlineKeyboardMarkup(row_width=2)
    for label, slug in items:
        kb.add(InlineKeyboardButton(label, callback_data=f"fin:cat:{flow}:{slug}"))
    kb.add(
        InlineKeyboardButton("✍️ Tulis sendiri", callback_data=f"fin:cat:{flow}:custom"),
        InlineKeyboardButton("🔙 Batal", callback_data="fin:home"),
    )
    return kb


def delete_list_keyboard(txs):
    kb = InlineKeyboardMarkup(row_width=1)
    for tx in txs[:8]:
        dt = parse_dt(tx["created_at"]).strftime("%d-%m-%Y %H:%M")
        label = f"🗑 {dt} | {tx['tx_type']} | {fmt_idr(int(tx['amount']))}"
        kb.add(InlineKeyboardButton(label, callback_data=f"fin:delask:{tx['id']}"))
    kb.add(
        InlineKeyboardButton("🔙 Kembali", callback_data="fin:home"),
        InlineKeyboardButton("🏠 Menu Utama", callback_data="main:menu"),
    )
    return kb


def confirm_delete_keyboard(tx_id: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Ya, hapus", callback_data=f"fin:delok:{tx_id}"),
        InlineKeyboardButton("❌ Batal", callback_data="fin:delete"),
    )
    kb.add(InlineKeyboardButton("🏠 Menu Utama", callback_data="main:menu"))
    return kb


def after_success_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Tambah Lagi", callback_data="fin:add"),
        InlineKeyboardButton("➖ Kurang Lagi", callback_data="fin:sub"),
    )
    kb.add(
        InlineKeyboardButton("💵 Menu Keuangan", callback_data="fin:home"),
        InlineKeyboardButton("🏠 Menu Utama", callback_data="main:menu"),
    )
    return kb


def _edit_or_send(bot, chat_id: int, message_id: int, text: str, markup=None):
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


def fetch_transactions(user_id: int, since=None, until=None, limit=None, desc=True):
    q = supabase.table(TX_TABLE).select("*").eq("user_id", user_id)
    if since is not None:
        q = q.gte("created_at", since.isoformat())
    if until is not None:
        q = q.lt("created_at", until.isoformat())
    q = q.order("created_at", desc=desc)
    if limit is not None:
        q = q.limit(limit)
    res = q.execute()
    return res.data or []


def insert_transaction(user_id: int, tx_type: str, amount: int, category: str, note: str, before: int, after: int):
    supabase.table(TX_TABLE).insert(
        {
            "user_id": user_id,
            "tx_type": tx_type,
            "amount": int(amount),
            "category": category,
            "note": note or None,
            "balance_before": int(before),
            "balance_after": int(after),
            "created_at": now_iso(),
        }
    ).execute()


def rebuild_ledger(user_id: int) -> int:
    txs = fetch_transactions(user_id, desc=False)
    balance = 0
    for tx in txs:
        before = balance
        amount = int(tx["amount"])
        if tx["tx_type"] == "plus":
            balance = before + amount
        else:
            balance = before - amount

        supabase.table(TX_TABLE).update(
            {
                "balance_before": before,
                "balance_after": balance,
            }
        ).eq("id", tx["id"]).execute()

    set_wallet_balance(user_id, balance)
    return balance


def delete_transaction(user_id: int, tx_id: str) -> bool:
    res = (
        supabase.table(TX_TABLE)
        .select("id")
        .eq("user_id", user_id)
        .eq("id", tx_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return False

    supabase.table(TX_TABLE).delete().eq("id", tx_id).eq("user_id", user_id).execute()
    rebuild_ledger(user_id)
    return True


def normalize_custom_category(value: str) -> str:
    value = clean_text(value)
    if not value:
        raise ValueError("Kategori tidak boleh kosong")
    return value[:50]


def category_from_slug(flow: str, slug: str) -> str:
    items = PLUS_CATEGORIES if flow == "plus" else MINUS_CATEGORIES
    lookup = {s: label for label, s in items}
    if slug == "custom":
        return ""
    return lookup.get(slug, slug.replace("_", " ").title())


def home_text(user_id: int) -> str:
    wallet = get_wallet(user_id)
    return (
        "💵 <b>Catat Keuangan</b>\n\n"
        f"Saldo saat ini: <b>{fmt_idr(wallet['balance'])}</b>\n\n"
        "Pilih menu di bawah."
    )


def graph_menu_text() -> str:
    return (
        "📊 <b>Grafik Keuangan</b>\n\n"
        "Pilih jenis grafik yang ingin ditampilkan."
    )


def add_amount_text(flow: str) -> str:
    label = "Tambah Saldo" if flow == "plus" else "Kurangi Saldo"
    contoh = "50000\n125000\n1000000" if flow == "plus" else "25000\n100000\n250000"
    return (
        f"{'➕' if flow == 'plus' else '➖'} <b>{label}</b>\n\n"
        "Kirim nominal angka saja.\n"
        f"Contoh:\n<code>{contoh}</code>"
    )


def category_prompt_text(flow: str) -> str:
    if flow == "plus":
        return (
            "➕ <b>Kategori Pemasukan</b>\n\n"
            "Pilih kategori pemasukan atau ketik sendiri."
        )
    return (
        "➖ <b>Kategori Pengeluaran</b>\n\n"
        "Pilih kategori pengeluaran atau ketik sendiri."
    )


def custom_category_prompt_text(flow: str) -> str:
    if flow == "plus":
        return (
            "➕ <b>Kategori Pemasukan</b>\n\n"
            "Kirim kategori sendiri.\n"
            "Contoh: Freelance, Hadiah, Jual Barang."
        )
    return (
        "➖ <b>Kategori Pengeluaran</b>\n\n"
        "Kirim kategori sendiri.\n"
        "Contoh: Makan, Transport, Tagihan."
    )


def note_prompt_text(flow: str, amount: int, category: str) -> str:
    icon = "➕" if flow == "plus" else "➖"
    label = "Tambah" if flow == "plus" else "Kurang"
    return (
        f"{icon} <b>{label}</b>\n\n"
        f"Nominal: <b>{fmt_idr(amount)}</b>\n"
        f"Kategori: <b>{escape(category)}</b>\n\n"
        "Kirim keterangan transaksi.\n"
        "Ketik <code>-</code> jika tidak ada."
    )


def success_text(flow: str, amount: int, category: str, note: str, balance_after: int) -> str:
    label = "Tambah" if flow == "plus" else "Kurang"
    note_show = note if note else "-"
    return (
        "✅ <b>Transaksi berhasil.</b>\n\n"
        f"Jenis      : <b>{label}</b>\n"
        f"Nominal    : <b>{fmt_idr(amount)}</b>\n"
        f"Kategori   : <b>{escape(category)}</b>\n"
        f"Keterangan : <b>{escape(note_show)}</b>\n\n"
        f"Saldo sekarang: <b>{fmt_idr(balance_after)}</b>"
    )


def recent_text(user_id: int) -> str:
    txs = fetch_transactions(user_id, limit=10, desc=True)
    if not txs:
        return "Belum ada transaksi."

    lines = ["🕒 <b>Transaksi Terakhir</b>", ""]
    for i, tx in enumerate(txs, start=1):
        dt = parse_dt(tx["created_at"]).strftime("%d-%m-%Y %H:%M")
        label = "Tambah" if tx["tx_type"] == "plus" else "Kurang"
        amount = fmt_idr(int(tx["amount"]))
        category = escape(tx.get("category") or "-")
        note = escape(tx.get("note") or "-")
        lines.append(
            f"{i}. {dt}\n"
            f"   {label} | {amount} | {category} | {note}\n"
        )
    return "\n".join(lines)


def delete_menu_text(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    txs = fetch_transactions(user_id, limit=8, desc=True)
    if not txs:
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("🔙 Kembali", callback_data="fin:home"),
            InlineKeyboardButton("🏠 Menu Utama", callback_data="main:menu"),
        )
        return "Belum ada transaksi untuk dihapus.", kb

    lines = ["🗑 <b>Hapus Transaksi</b>", "", "Pilih transaksi yang mau dihapus."]
    for tx in txs:
        dt = parse_dt(tx["created_at"]).strftime("%d-%m-%Y %H:%M")
        label = "Tambah" if tx["tx_type"] == "plus" else "Kurang"
        amount = fmt_idr(int(tx["amount"]))
        category = escape(tx.get("category") or "-")
        lines.append(f"• {dt} | {label} | {amount} | {category}")

    return "\n".join(lines), delete_list_keyboard(txs)


def _month_list(count: int):
    now = datetime.now().astimezone()
    base = month_start(now)
    months = []
    for offset in range(count - 1, -1, -1):
        months.append(shift_month(base, -offset))
    return months


def aggregate_daily(user_id: int, days: int = 30):
    end_date = datetime.now().astimezone().date()
    start_date = end_date - timedelta(days=days - 1)

    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=datetime.now().astimezone().tzinfo)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, tzinfo=datetime.now().astimezone().tzinfo) + timedelta(days=1)

    txs = fetch_transactions(user_id, since=start_dt, until=end_dt, desc=False)

    buckets = {}
    day = start_date
    for _ in range(days):
        buckets[day] = {"plus": 0, "minus": 0}
        day += timedelta(days=1)

    for tx in txs:
        day_key = parse_dt(tx["created_at"]).date()
        if day_key not in buckets:
            continue
        buckets[day_key][tx["tx_type"]] += int(tx["amount"])

    labels = [d.strftime("%d/%m") for d in buckets.keys()]
    income = [v["plus"] for v in buckets.values()]
    expense = [v["minus"] for v in buckets.values()]
    net = [v["plus"] - v["minus"] for v in buckets.values()]
    return labels, income, expense, net


def aggregate_monthly(user_id: int, months: int = 6):
    month_list = _month_list(months)
    start_dt = month_list[0]
    end_dt = shift_month(month_list[-1], 1)

    txs = fetch_transactions(user_id, since=start_dt, until=end_dt, desc=False)

    buckets = {}
    for m in month_list:
        buckets[m.strftime("%Y-%m")] = {"plus": 0, "minus": 0}

    for tx in txs:
        key = parse_dt(tx["created_at"]).strftime("%Y-%m")
        if key not in buckets:
            continue
        buckets[key][tx["tx_type"]] += int(tx["amount"])

    labels = [datetime.strptime(k, "%Y-%m").strftime("%b %Y") for k in buckets.keys()]
    income = [v["plus"] for v in buckets.values()]
    expense = [v["minus"] for v in buckets.values()]
    net = [v["plus"] - v["minus"] for v in buckets.values()]
    return labels, income, expense, net


def aggregate_accumulation(user_id: int, months: int = 12):
    labels, income, expense, net = aggregate_monthly(user_id, months=months)
    running = 0
    accum = []
    for value in net:
        running += value
        accum.append(running)
    return labels, accum


def aggregate_categories(user_id: int, months: int = 6):
    month_list = _month_list(months)
    start_dt = month_list[0]
    end_dt = shift_month(month_list[-1], 1)
    txs = fetch_transactions(user_id, since=start_dt, until=end_dt, desc=False)

    categories = defaultdict(int)
    for tx in txs:
        if tx["tx_type"] != "minus":
            continue
        category = clean_text(tx.get("category") or "Lainnya") or "Lainnya"
        categories[category] += int(tx["amount"])

    if not categories:
        return [], []

    items = sorted(categories.items(), key=lambda x: x[1], reverse=True)
    top = items[:6]
    if len(items) > 6:
        others = sum(v for _, v in items[6:])
        top.append(("Lainnya", others))

    labels = [k for k, _ in top]
    values = [v for _, v in top]
    return labels, values


def chart_trend_30d(user_id: int):
    labels, income, expense, net = aggregate_daily(user_id, days=30)

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(labels, income, marker="o", label="Pemasukan")
    ax.plot(labels, expense, marker="o", label="Pengeluaran")
    ax.plot(labels, net, marker="o", label="Net")
    ax.set_title("Tren 30 Hari")
    ax.set_xlabel("Tanggal")
    ax.set_ylabel("Nominal")
    ax.legend()
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "tren_30_hari.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def chart_monthly_compare(user_id: int, months: int = 6):
    labels, income, expense, _ = aggregate_monthly(user_id, months=months)
    x = list(range(len(labels)))
    width = 0.35

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.bar([i - width / 2 for i in x], income, width, label="Pemasukan")
    ax.bar([i + width / 2 for i in x], expense, width, label="Pengeluaran")
    ax.set_title("Perbandingan Bulanan")
    ax.set_xlabel("Bulan")
    ax.set_ylabel("Nominal")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45)
    ax.legend()
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "per_bulan.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def chart_accumulation(user_id: int, months: int = 12):
    labels, accum = aggregate_accumulation(user_id, months=months)

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(labels, accum, marker="o", linewidth=2)
    ax.set_title("Akumulasi Bersih")
    ax.set_xlabel("Bulan")
    ax.set_ylabel("Saldo Bersih")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "akumulasi.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def chart_category(user_id: int, months: int = 6):
    labels, values = aggregate_categories(user_id, months=months)
    if not labels:
        return None

    fig, ax = plt.subplots(figsize=(10, 10))
    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.set_title("Pengeluaran per Kategori")
    ax.axis("equal")
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "kategori.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def report_bytes(user_id: int):
    wallet = get_wallet(user_id)
    all_txs = fetch_transactions(user_id, desc=False)
    recent = fetch_transactions(user_id, limit=7, desc=True)

    total_income = sum(int(tx["amount"]) for tx in all_txs if tx["tx_type"] == "plus")
    total_expense = sum(int(tx["amount"]) for tx in all_txs if tx["tx_type"] == "minus")
    net = total_income - total_expense

    expense_categories = defaultdict(int)
    for tx in all_txs:
        if tx["tx_type"] != "minus":
            continue
        category = clean_text(tx.get("category") or "Lainnya") or "Lainnya"
        expense_categories[category] += int(tx["amount"])

    top_category = "-"
    top_value = 0
    if expense_categories:
        top_category, top_value = max(expense_categories.items(), key=lambda x: x[1])

    fig, ax = plt.subplots(figsize=(12, 9))
    ax.axis("off")

    lines = [
        "LAPORAN KEUANGAN",
        "",
        f"Saldo Saat Ini   : {fmt_idr(int(wallet['balance']))}",
        f"Total Pemasukan  : {fmt_idr(total_income)}",
        f"Total Pengeluaran : {fmt_idr(total_expense)}",
        f"Selisih Bersih   : {fmt_idr(net)}",
        f"Jumlah Transaksi  : {len(all_txs)}",
        f"Top Kategori      : {top_category} ({fmt_idr(top_value)})",
        "",
        "Transaksi Terakhir:",
    ]

    for i, tx in enumerate(recent, start=1):
        dt = parse_dt(tx["created_at"]).strftime("%d-%m-%Y %H:%M")
        label = "Tambah" if tx["tx_type"] == "plus" else "Kurang"
        amount = fmt_idr(int(tx["amount"]))
        category = tx.get("category") or "-"
        note = tx.get("note") or "-"
        lines.append(f"{i}. {dt} | {label} | {amount} | {category} | {note}")

    fig.text(0.03, 0.97, "\n".join(lines), va="top", fontsize=11, family="monospace")
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "laporan_keuangan.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def show_finance_home(bot, chat_id: int, message_id: int | None, user_id: int):
    _edit_or_send(bot, chat_id, message_id, home_text(user_id), finance_home_keyboard())


def show_graph_menu(bot, chat_id: int, message_id: int | None):
    _edit_or_send(bot, chat_id, message_id, graph_menu_text(), graph_menu_keyboard())


def set_pending(user_id: int, chat_id: int, message_id: int, flow: str, step: str, amount: int = 0, category: str = ""):
    PENDING[user_id] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "flow": flow,
        "step": step,
        "amount": amount,
        "category": category,
    }


def clear_pending(user_id: int):
    PENDING.pop(user_id, None)


def _category_prompt_markup(flow: str):
    return category_keyboard(flow)


def _note_markup():
    return cancel_keyboard()


def _send_chart(bot, call, bio, caption: str, back_callback: str):
    bot.send_photo(
        call.message.chat.id,
        photo=bio,
        caption=caption,
        reply_markup=photo_keyboard(back_callback),
    )


def register_finance(bot):
    @bot.callback_query_handler(func=lambda call: call.data.startswith("fin:"))
    def finance_router(call):
        user_id = call.from_user.id
        if not allowed(user_id):
            bot.answer_callback_query(call.id, "Akses ditolak")
            return

        data = call.data
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        if data == "fin:home":
            clear_pending(user_id)
            show_finance_home(bot, chat_id, message_id, user_id)
            bot.answer_callback_query(call.id)
            return

        if data == "fin:graph_menu":
            clear_pending(user_id)
            show_graph_menu(bot, chat_id, message_id)
            bot.answer_callback_query(call.id)
            return

        if data == "fin:add":
            clear_pending(user_id)
            set_pending(user_id, chat_id, message_id, "plus", "amount")
            _edit_or_send(bot, chat_id, message_id, add_amount_text("plus"), cancel_keyboard())
            bot.answer_callback_query(call.id)
            return

        if data == "fin:sub":
            clear_pending(user_id)
            set_pending(user_id, chat_id, message_id, "minus", "amount")
            _edit_or_send(bot, chat_id, message_id, add_amount_text("minus"), cancel_keyboard())
            bot.answer_callback_query(call.id)
            return

        if data == "fin:recent":
            clear_pending(user_id)
            _edit_or_send(bot, chat_id, message_id, recent_text(user_id), finance_home_keyboard())
            bot.answer_callback_query(call.id)
            return

        if data == "fin:delete":
            clear_pending(user_id)
            text, kb = delete_menu_text(user_id)
            _edit_or_send(bot, chat_id, message_id, text, kb)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("fin:delask:"):
            tx_id = data.split(":", 2)[2]
            clear_pending(user_id)
            txs = fetch_transactions(user_id, limit=50, desc=True)
            tx = next((item for item in txs if item["id"] == tx_id), None)
            if not tx:
                _edit_or_send(bot, chat_id, message_id, "Transaksi tidak ditemukan.", finance_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            dt = parse_dt(tx["created_at"]).strftime("%d-%m-%Y %H:%M")
            label = "Tambah" if tx["tx_type"] == "plus" else "Kurang"
            amount = fmt_idr(int(tx["amount"]))
            category = tx.get("category") or "-"
            note = tx.get("note") or "-"
            text = (
                "Yakin mau hapus transaksi ini?\n\n"
                f"{dt}\n"
                f"{label} | {amount} | {category} | {note}"
            )
            _edit_or_send(bot, chat_id, message_id, text, confirm_delete_keyboard(tx_id))
            bot.answer_callback_query(call.id)
            return

        if data.startswith("fin:delok:"):
            tx_id = data.split(":", 2)[2]
            clear_pending(user_id)
            ok = delete_transaction(user_id, tx_id)
            if not ok:
                _edit_or_send(bot, chat_id, message_id, "Transaksi tidak ditemukan.", finance_home_keyboard())
                bot.answer_callback_query(call.id, "Tidak ditemukan")
                return

            wallet = get_wallet(user_id)
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                f"✅ Transaksi berhasil dihapus.\n\nSaldo sekarang: <b>{fmt_idr(int(wallet['balance']))}</b>",
                after_success_keyboard(),
            )
            bot.answer_callback_query(call.id, "Dihapus")
            return

        if data == "fin:report":
            clear_pending(user_id)
            bot.answer_callback_query(call.id, "Membuat laporan...")
            bio = report_bytes(user_id)
            _send_chart(bot, call, bio, "📄 Laporan Keuangan", "fin:home")
            show_finance_home(bot, chat_id, message_id, user_id)
            return

        if data == "fin:graph_trend":
            clear_pending(user_id)
            bot.answer_callback_query(call.id, "Membuat grafik...")
            bio = chart_trend_30d(user_id)
            _send_chart(bot, call, bio, "📈 Tren 30 Hari", "fin:graph_menu")
            show_graph_menu(bot, chat_id, message_id)
            return

        if data == "fin:graph_monthly":
            clear_pending(user_id)
            bot.answer_callback_query(call.id, "Membuat grafik...")
            bio = chart_monthly_compare(user_id, months=6)
            _send_chart(bot, call, bio, "📊 Perbandingan Bulanan", "fin:graph_menu")
            show_graph_menu(bot, chat_id, message_id)
            return

        if data == "fin:graph_accum":
            clear_pending(user_id)
            bot.answer_callback_query(call.id, "Membuat grafik...")
            bio = chart_accumulation(user_id, months=12)
            _send_chart(bot, call, bio, "📈 Akumulasi Bersih 12 Bulan", "fin:graph_menu")
            show_graph_menu(bot, chat_id, message_id)
            return

        if data == "fin:graph_category":
            clear_pending(user_id)
            bot.answer_callback_query(call.id, "Membuat grafik...")
            bio = chart_category(user_id, months=6)
            if bio is None:
                _edit_or_send(bot, chat_id, message_id, "Belum ada data kategori pengeluaran.", graph_menu_keyboard())
                return
            _send_chart(bot, call, bio, "🥧 Pengeluaran per Kategori", "fin:graph_menu")
            show_graph_menu(bot, chat_id, message_id)
            return

        if data.startswith("fin:cat:"):
            clear_pending(user_id)
            parts = data.split(":")
            if len(parts) != 4:
                bot.answer_callback_query(call.id, "Data kategori tidak valid")
                return

            _, _, flow, slug = parts
            state = PENDING.get(user_id)
            if not state or state.get("flow") != flow or state.get("step") not in {"category_pick", "category_custom"}:
                bot.answer_callback_query(call.id, "Langkah kategori sudah lewat")
                return

            if slug == "custom":
                state["step"] = "category_custom"
                PENDING[user_id] = state
                _edit_or_send(bot, chat_id, message_id, custom_category_prompt_text(flow), cancel_keyboard())
                bot.answer_callback_query(call.id)
                return

            category = category_from_slug(flow, slug)
            state["category"] = category
            state["step"] = "note"
            PENDING[user_id] = state
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                note_prompt_text(flow, state["amount"], category),
                _note_markup(),
            )
            bot.answer_callback_query(call.id)
            return

        if data == "fin:cancel":
            clear_pending(user_id)
            show_finance_home(bot, chat_id, message_id, user_id)
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

        text = (message.text or "").strip()
        chat_id = state["chat_id"]
        message_id = state["message_id"]
        flow = state["flow"]
        step = state["step"]

        if step == "amount":
            try:
                amount = parse_amount(text)
            except Exception:
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "❌ Nominal tidak valid.\nKirim angka saja, contoh: <code>50000</code>",
                    cancel_keyboard(),
                )
                return

            state["amount"] = amount
            state["step"] = "category_pick"
            PENDING[user_id] = state
            _edit_or_send(bot, chat_id, message_id, category_prompt_text(flow), _category_prompt_markup(flow))
            return

        if step == "category_custom":
            try:
                category = normalize_custom_category(text)
            except Exception:
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "❌ Kategori tidak boleh kosong.\nKirim kategori yang benar.",
                    cancel_keyboard(),
                )
                return

            state["category"] = category
            state["step"] = "note"
            PENDING[user_id] = state
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                note_prompt_text(flow, state["amount"], category),
                _note_markup(),
            )
            return

        if step == "note":
            note = clean_text(text)
            if note == "-":
                note = ""

            amount = int(state["amount"])
            category = clean_text(state.get("category") or "-") or "-"
            wallet = get_wallet(user_id)
            before = int(wallet["balance"])
            after = before + amount if flow == "plus" else before - amount

            if after < 0:
                clear_pending(user_id)
                _edit_or_send(
                    bot,
                    chat_id,
                    message_id,
                    "❌ Saldo tidak cukup untuk transaksi ini.",
                    finance_home_keyboard(),
                )
                return

            insert_transaction(user_id, flow, amount, category, note, before, after)
            rebuild_ledger(user_id)
            wallet = get_wallet(user_id)

            clear_pending(user_id)
            _edit_or_send(
                bot,
                chat_id,
                message_id,
                success_text(flow, amount, category, note, int(wallet["balance"])),
                after_success_keyboard(),
            )
            return

    return handle_text
