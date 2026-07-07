import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BOT_TOKEN, OWNER_ID
from features.finance import register_finance

if not BOT_TOKEN:
    raise RuntimeError("TOKEN_BOT belum diisi")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

handle_finance_text = register_finance(bot)


def allowed(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID


def main_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Keuangan", callback_data="main:finance"))
    return kb


def main_text():
    return (
        "👋 <b>Selamat datang.</b>\n\n"
        "Ini adalah asisten pribadi multifungsi.\n"
        "Silakan pilih menu di bawah."
    )


def show_main(chat_id: int, message_id: int | None = None):
    if message_id:
        try:
            bot.edit_message_text(
                text=main_text(),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=main_keyboard(),
                parse_mode="HTML",
            )
            return
        except Exception:
            pass

    bot.send_message(chat_id, main_text(), reply_markup=main_keyboard(), parse_mode="HTML")


@bot.message_handler(commands=["start"])
def start(message):
    if not allowed(message.from_user.id):
        return
    show_main(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "main:menu")
def back_main(call):
    if not allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "Akses ditolak")
        return
    show_main(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.message_handler(content_types=["text"], func=lambda m: not m.text.startswith("/"))
def free_text(message):
    if not allowed(message.from_user.id):
        return
    handle_finance_text(message)


if __name__ == "__main__":
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
