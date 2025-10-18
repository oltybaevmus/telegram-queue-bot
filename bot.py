import json
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode

TOKEN = "8246901324:AAH3FHDKTJpVwPi66aZGU1PBv6R22WxPQL0"

QUEUE_FILE = "queue.json"

bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

def load_queue():
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_queue(queue):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

@dp.message(Command("standup"))
async def standup(msg: types.Message):
    q = load_queue()
    user = msg.from_user.username

    if user in q:
        return await msg.reply("Ты уже в очереди ✅")

    q.append(user)
    save_queue(q)
    await msg.reply(f"Добавил тебя в очередь. Сейчас в очереди: {len(q)} человек(а).")

@dp.message(Command("delete"))
async def delete_me(msg: types.Message):
    q = load_queue()
    user = msg.from_user.username

    if user not in q:
        return await msg.reply("Тебя нет в очереди 😉")

    q.remove(user)
    save_queue(q)
    await msg.reply("Удалил тебя из очереди.")

@dp.message(Command("list"))
async def list_queue(msg: types.Message):
    q = load_queue()
    if not q:
        return await msg.reply("Сейчас никто не в очереди. Напиши /standup, чтобы начать.")
    txt = "\n".join([f"{i+1}) @{u}" for i, u in enumerate(q)])
    await msg.reply(f"Текущая очередь:\n{txt}")

@dp.message(Command("finished"))
async def finished(msg: types.Message):
    q = load_queue()
    user = msg.from_user.username

    if not q:
        return await msg.reply("Очередь пустая.")

    if q[0] != user:
        return await msg.reply("Сначала дождись своей очереди 🙂")

    # удаляем первого
    q.pop(0)
    save_queue(q)

    # если теперь очередь не пустая — тегаем следующего
    if q:
        next_user = q[0]
        await msg.answer(f"@{next_user} твоя очередь")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

