from collections import defaultdict, OrderedDict
from datetime import datetime
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import supabase

WALLET_TABLE = "finance_wallets"
TX_TABLE = "finance_transactions"


def _fmt_idr(value: int) -> str:
    return f"Rp{int(value):,}".replace(",", ".")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _month_label(key: str) -> str:
    dt = datetime.strptime(key, "%Y-%m")
    return dt.strftime("%b %Y")


def _safe_category(value):
    if value is None:
        return "Lainnya"
    value = str(value).strip()
    return value if value else "Lainnya"


def _get_wallet(user_id: int):
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
    return {"balance": 0}


def _get_transactions(user_id: int):
    res = (
        supabase.table(TX_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


def _build_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📈 Tren Bulanan", callback_data="fplus:trend"),
        InlineKeyboardButton("📊 Akumulasi", callback_data="fplus:accum"),
    )
    kb.add(
        InlineKeyboardButton("🥧 Kategori", callback_data="fplus:cats"),
        InlineKeyboardButton("🧾 Ringkasan", callback_data="fplus:summary"),
    )
    kb.add(InlineKeyboardButton("🔙 Kembali", callback_data="finance:menu"))
    return kb


def _send_or_edit(bot, call, text, markup=None):
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")


def _plot_bytes(title, xlabel, ylabel, labels, values, kind="bar"):
    fig, ax = plt.subplots(figsize=(12, 6))
    if kind == "bar":
        ax.bar(labels, values)
    elif kind == "line":
        ax.plot(labels, values, marker="o")
    elif kind == "pie":
        ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
        ax.axis("equal")
    ax.set_title(title)
    if kind != "pie":
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "chart.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def _build_monthly_summary(user_id: int):
    txs = _get_transactions(user_id)
    month_map = OrderedDict()

    for tx in txs:
        dt = _parse_dt(tx["created_at"])
        key = _month_key(dt)
        month_map.setdefault(key, {"plus": 0, "minus": 0, "net": 0, "count": 0})
        amount = int(tx["amount"])
        if tx["tx_type"] == "plus":
            month_map[key]["plus"] += amount
            month_map[key]["net"] += amount
        else:
            month_map[key]["minus"] += amount
            month_map[key]["net"] -= amount
        month_map[key]["count"] += 1

    return month_map


def _build_category_summary(user_id: int):
    txs = _get_transactions(user_id)
    cat = defaultdict(int)
    for tx in txs:
        if tx["tx_type"] != "minus":
            continue
        category = _safe_category(tx.get("category"))
        cat[category] += int(tx["amount"])
    return dict(sorted(cat.items(), key=lambda x: x[1], reverse=True))


def _build_accumulation(user_id: int):
    by_month = _build_monthly_summary(user_id)
    accum = []
    total = 0
    for key, item in by_month.items():
        total += item["net"]
        accum.append((key, total))
    return accum


def _trend_chart(user_id: int):
    by_month = _build_monthly_summary(user_id)
    if not by_month:
        return None

    labels = [_month_label(k) for k in by_month.keys()]
    income = [v["plus"] for v in by_month.values()]
    expense = [v["minus"] for v in by_month.values()]
    net = [v["net"] for v in by_month.values()]

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(labels, income, marker="o", label="Pemasukan")
    ax.plot(labels, expense, marker="o", label="Pengeluaran")
    ax.plot(labels, net, marker="o", label="Net")
    ax.set_title("Tren Keuangan Bulanan")
    ax.set_xlabel("Bulan")
    ax.set_ylabel("Nominal")
    ax.legend()
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()

    bio = BytesIO()
    bio.name = "trend.jpeg"
    fig.savefig(bio, format="jpeg", dpi=160, bbox_inches="tight")
    plt.close(fig)
    bio.seek(0)
    return bio


def _accum_chart(user_id: int):
    data = _build_accumulation(user_id)
    if not data:
        return None

    labels = [_month_label(k) for k, _ in data]
    values = [v for _, v in data]
    return _plot_bytes(
        "Akumulasi Saldo Bersih",
        "Bulan",
        "Saldo Bersih",
        labels,
        values,
        kind="line",
    )


def _category_chart(user_id: int):
    data = _build_category_summary(user_id)
    if not data:
        return None

    labels = list(data.keys())[:8]
    values = list(data.values())[:8]
    return _plot_bytes(
        "Pengeluaran per Kategori",
        "Kategori",
        "Nominal",
        labels,
        values,
        kind="bar",
    )


def _summary_text(user_id: int) -> str:
    wallet = _get_wallet(user_id)
    by_month = _build_monthly_summary(user_id)
    cats = _build_category_summary(user_id)

    total_plus = sum(v["plus"] for v in by_month.values())
    total_minus = sum(v["minus"] for v in by_month.values())
    tx_count = sum(v["count"] for v in by_month.values())

    top_cat = "-"
    top_cat_val = 0
    if cats:
        top_cat, top_cat_val = next(iter(cats.items()))

    last_month = "-"
    if by_month:
        last_key = next(reversed(by_month))
        last_month = f"{_month_label(last_key)} | net {_fmt_idr(by_month[last_key]['net'])}"

    return (
        "🧠 <b>Ringkasan Keuangan</b>\n\n"
        f"Saldo saat ini : <b>{_fmt_idr(wallet.get('balance', 0))}</b>\n"
        f"Total masuk    : <b>{_fmt_idr(total_plus)}</b>\n"
        f"Total keluar   : <b>{_fmt_idr(total_minus)}</b>\n"
        f"Jumlah transaksi: <b>{tx_count}</b>\n"
        f"Bulan terakhir  : <b>{last_month}</b>\n"
        f"Kategori terbesar: <b>{top_cat}</b> ({_fmt_idr(top_cat_val)})"
    )


def register_finance_plus(bot):
    @bot.message_handler(commands=["analisis_keuangan"])
    def open_finance_plus(message):
        bot.send_message(
            message.chat.id,
            "📊 <b>Analisis Keuangan</b>\n\nPilih ringkasan, tren, akumulasi, atau kategori.",
            reply_markup=_build_menu(),
            parse_mode="HTML",
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith("fplus:"))
    def finance_plus_router(call):
        user_id = call.from_user.id
        data = call.data

        if data == "fplus:summary":
            _send_or_edit(bot, call, _summary_text(user_id), _build_menu())
            bot.answer_callback_query(call.id)
            return

        if data == "fplus:trend":
            chart = _trend_chart(user_id)
            if chart is None:
                _send_or_edit(bot, call, "Belum ada data untuk tren bulanan.", _build_menu())
                bot.answer_callback_query(call.id)
                return
            bot.send_photo(call.message.chat.id, photo=chart, caption="📈 Tren Keuangan Bulanan")
            _send_or_edit(bot, call, "Grafik tren sudah dikirim.", _build_menu())
            bot.answer_callback_query(call.id)
            return

        if data == "fplus:accum":
            chart = _accum_chart(user_id)
            if chart is None:
                _send_or_edit(bot, call, "Belum ada data untuk akumulasi.", _build_menu())
                bot.answer_callback_query(call.id)
                return
            bot.send_photo(call.message.chat.id, photo=chart, caption="📈 Akumulasi Keuangan")
            _send_or_edit(bot, call, "Grafik akumulasi sudah dikirim.", _build_menu())
            bot.answer_callback_query(call.id)
            return

        if data == "fplus:cats":
            chart = _category_chart(user_id)
            if chart is None:
                _send_or_edit(bot, call, "Belum ada data kategori pengeluaran.", _build_menu())
                bot.answer_callback_query(call.id)
                return
            bot.send_photo(call.message.chat.id, photo=chart, caption="🥧 Pengeluaran per Kategori")
            _send_or_edit(bot, call, "Grafik kategori sudah dikirim.", _build_menu())
            bot.answer_callback_query(call.id)
            return
