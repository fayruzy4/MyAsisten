import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BOT_TOKEN, OWNER_ID
from features.finance import (
    register_finance,
    show_finance_home,
    clear_pending as clear_finance_pending,
)
from features.hutang import (
    register_hutang,
    show_hutang_home,
    clear_pending as clear_hutang_pending,
)
from features.target import (
    register_target,
    show_target_home,
    clear_pending as clear_target_pending,
)
from features.habit import (
    register_habit,
    show_habit_home,
    clear_pending as clear_habit_pending,
)
from features.capsule import (
    register_capsule,
    show_capsule_home,
    clear_pending as clear_capsule_pending,
)
from features.download import (
    register_download,
    clear_pending as clear_download_pending,
)
from features.server_monitor import (
    register_server_monitor,
    show_monitor_home,
    clear_pending as clear_server_monitor_pending,
)

if not BOT_TOKEN:
    raise RuntimeError("TOKEN_BOT belum diisi")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

handle_finance_text = register_finance(bot)
handle_hutang_text = register_hutang(bot)
handle_target_text = register_target(bot)
handle_habit_text = register_habit(bot)
handle_capsule_text = register_capsule(bot)
register_download(bot)


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def main_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💰 Keuangan", callback_data="main:keuangan"))
    kb.add(InlineKeyboardButton("🚀 Produktivitas", callback_data="main:produktif"))
    kb.add(InlineKeyboardButton("🛠️ Utilitas", callback_data="main:utilitas"))
    return kb


def keuangan_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💵 Catat Keuangan", callback_data="main:catat_keuangan"))
    kb.add(InlineKeyboardButton("💳 Catat Hutang", callback_data="main:hutang"))
    kb.add(InlineKeyboardButton("🎯 Target", callback_data="main:target"))
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def produktivitas_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("📅 Habit Tracker", callback_data="main:habit"))
    kb.add(InlineKeyboardButton("⏳ Kapsul Waktu", callback_data="main:capsule"))
    kb.add(InlineKeyboardButton("🎓 Berkas Beasiswa", callback_data="main:soon:beasiswa"))
    kb.add(InlineKeyboardButton("🎯 Target Masa Depan", callback_data="main:soon:future"))
    kb.add(InlineKeyboardButton("🏠 Dashboard", callback_data="main:menu"))
    return kb


def main_text():
    return (
        "👋 <b>Selamat datang.</b>\n\n"
        "Ini adalah asisten pribadi multifungsi.\n"
        "Silakan pilih menu di bawah."
    )


def keuangan_text():
    return (
        "💰 <b>Keuangan</b>\n\n"
        "Silakan pilih submenu yang ingin digunakan."
    )


def produktivitas_text():
    return (
        "🚀 <b>Produktivitas</b>\n\n"
        "Silakan pilih fitur yang ingin digunakan."
    )


def show_main(chat_id: int, message_id: int | None = None):
    if message_id:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=main_text(),
                reply_markup=main_keyboard(),
                parse_mode="HTML",
            )
            return
        except Exception:
            pass

    bot.send_message(chat_id, main_text(), reply_markup=main_keyboard(), parse_mode="HTML")


def show_keuangan_menu(chat_id: int, message_id: int | None = None):
    if message_id:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=keuangan_text(),
                reply_markup=keuangan_keyboard(),
                parse_mode="HTML",
            )
            return
        except Exception:
            pass

    bot.send_message(chat_id, keuangan_text(), reply_markup=keuangan_keyboard(), parse_mode="HTML")


def show_produktivitas_menu(chat_id: int, message_id: int | None = None):
    if message_id:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=produktivitas_text(),
                reply_markup=produktivitas_keyboard(),
                parse_mode="HTML",
            )
            return
        except Exception:
            pass

    bot.send_message(chat_id, produktivitas_text(), reply_markup=produktivitas_keyboard(), parse_mode="HTML")


@bot.message_handler(commands=["start"])
def start(message):
    if not allowed(message.from_user.id):
        return
    clear_finance_pending(message.from_user.id)
    clear_hutang_pending(message.from_user.id)
    clear_target_pending(message.from_user.id)
    clear_habit_pending(message.from_user.id)
    clear_capsule_pending(message.from_user.id)
    clear_download_pending(message.from_user.id)
    show_main(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "main:menu")
def back_main(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    clear_finance_pending(call.from_user.id)
    clear_hutang_pending(call.from_user.id)
    clear_target_pending(call.from_user.id)
    clear_habit_pending(call.from_user.id)
    clear_capsule_pending(call.from_user.id)
    clear_download_pending(call.from_user.id)
    show_main(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data in ("main:keuangan", "main:finance"))
def open_keuangan(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    clear_finance_pending(call.from_user.id)
    clear_hutang_pending(call.from_user.id)
    clear_target_pending(call.from_user.id)
    clear_habit_pending(call.from_user.id)
    clear_capsule_pending(call.from_user.id)
    clear_download_pending(call.from_user.id)
    show_keuangan_menu(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "main:catat_keuangan")
def open_catat_keuangan(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    clear_finance_pending(call.from_user.id)
    clear_hutang_pending(call.from_user.id)
    clear_target_pending(call.from_user.id)
    clear_habit_pending(call.from_user.id)
    clear_capsule_pending(call.from_user.id)
    clear_download_pending(call.from_user.id)
    show_finance_home(bot, call.message.chat.id, call.message.message_id, call.from_user.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "main:hutang")
def open_hutang(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    clear_finance_pending(call.from_user.id)
    clear_hutang_pending(call.from_user.id)
    clear_target_pending(call.from_user.id)
    clear_habit_pending(call.from_user.id)
    clear_capsule_pending(call.from_user.id)
    clear_download_pending(call.from_user.id)
    show_hutang_home(bot, call.message.chat.id, call.message.message_id, call.from_user.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "main:target")
def open_target(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    clear_finance_pending(call.from_user.id)
    clear_hutang_pending(call.from_user.id)
    clear_target_pending(call.from_user.id)
    clear_habit_pending(call.from_user.id)
    clear_capsule_pending(call.from_user.id)
    clear_download_pending(call.from_user.id)
    show_target_home(bot, call.message.chat.id, call.message.message_id, call.from_user.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data in ("main:produktif", "main:produktivitas"))
def open_produktivitas(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    clear_finance_pending(call.from_user.id)
    clear_hutang_pending(call.from_user.id)
    clear_target_pending(call.from_user.id)
    clear_habit_pending(call.from_user.id)
    clear_capsule_pending(call.from_user.id)
    clear_download_pending(call.from_user.id)
    show_produktivitas_menu(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "main:habit")
def open_habit(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    clear_finance_pending(call.from_user.id)
    clear_hutang_pending(call.from_user.id)
    clear_target_pending(call.from_user.id)
    clear_habit_pending(call.from_user.id)
    clear_capsule_pending(call.from_user.id)
    clear_download_pending(call.from_user.id)
    show_habit_home(bot, call.message.chat.id, call.message.message_id, call.from_user.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "main:capsule")
def open_capsule(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    clear_finance_pending(call.from_user.id)
    clear_hutang_pending(call.from_user.id)
    clear_target_pending(call.from_user.id)
    clear_habit_pending(call.from_user.id)
    clear_capsule_pending(call.from_user.id)
    clear_download_pending(call.from_user.id)
    show_capsule_home(bot, call.message.chat.id, call.message.message_id, call.from_user.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("main:soon:"))
def soon_feature(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    bot.answer_callback_query(call.id, "Fitur ini sedang disiapkan.")


@bot.message_handler(content_types=["text"], func=lambda m: not m.text.startswith("/"))
def route_text(message):
    if not allowed(message.from_user.id):
        return
    handle_finance_text(message)
    handle_hutang_text(message)
    handle_target_text(message)
    handle_habit_text(message)
    handle_capsule_text(message)


if __name__ == "__main__":
    print("MyAsisten aktif")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
