import json
import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils import executor

# -------------------------------
# ВСТАВЬ СВОЙ ТОКЕН
TOKEN = "8246901324:AAH3FHDKTJpVwPi66aZGU1PBv6R22WxPQL0"
# -------------------------------

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# Файл для хранения очереди
QUEUE_FILE = "queue.json"

# Глобальные переменные
queue = []
active_chat_id = None
active_thread_id = None
message_with_buttons_id = None

# -------------------------------
# Вспомогательные функции
# -------------------------------
def load_queue():
    global queue
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            queue = json.load(f)
    else:
        queue = []

def save_queue():
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

def build_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("Встать в очередь", callback_data="join"),
        InlineKeyboardButton("Покинуть очередь", callback_data="leave"),
        InlineKeyboardButton("Список", callback_data="list"),
        InlineKeyboardButton("Передумал", callback_data="undo"),
    )
    return keyboard

def format_queue():
    if not queue:
        return "Очередь пуста"
    return "\n".join(f"{i+1}) @{username}" for i, username in enumerate(queue))

# -------------------------------
# Обработчики
# -------------------------------
@dp.message_handler(commands=["start"])
async def start_handler(message: types.Message):
    global active_chat_id, active_thread_id, message_with_buttons_id
    active_chat_id = message.chat.id
    active_thread_id = message.message_thread_id
    load_queue()
    keyboard = build_keyboard()
    sent_message = await message.reply("Очередь в репорт", reply_markup=keyboard)
    message_with_buttons_id = sent_message.message_id
    # Отправляем ID темы и чата пользователю для информации
    await message.reply(f"chat_id: {active_chat_id}\nthread_id: {active_thread_id}")

@dp.callback_query_handler(lambda c: True)
async def callback_handler(callback_query: types.CallbackQuery):
    global queue
    user = callback_query.from_user.username or callback_query.from_user.first_name
    chat_id = callback_query.message.chat.id
    thread_id = callback_query.message.message_thread_id

    # Проверяем, что мы работаем только в одной теме
    if chat_id != active_chat_id or thread_id != active_thread_id:
        await callback_query.answer("Этот бот работает только в своей теме", show_alert=True)
        return

    load_queue()

    if callback_query.data == "join":
        if user not in queue:
            queue.append(user)
            save_queue()
        await callback_query.answer()  # тихо, без ответа

    elif callback_query.data in ["leave", "undo"]:
        if user in queue:
            queue.remove(user)
            save_queue()
            # Если есть следующий в очереди — уведомляем
            if queue:
                next_user = queue[0]
                await bot.send_message(chat_id, f"@{next_user}, бери отчет", message_thread_id=thread_id)
        await callback_query.answer()  # тихо

    elif callback_query.data == "list":
        await bot.send_message(chat_id, format_queue(), message_thread_id=thread_id)
        await callback_query.answer()  # тихо

# -------------------------------
# Запуск
# -------------------------------
if __name__ == "__main__":
    load_queue()
    executor.start_polling(dp, skip_updates=True)
