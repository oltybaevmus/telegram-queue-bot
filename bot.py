import asyncio
import json
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

TOKEN = "8246901324:AAH3FHDKTJpVwPi66aZGU1PBv6R22WxPQL0"

bot = Bot(token=TOKEN)
dp = Dispatcher()

QUEUE_FILE = "queue.json"

# Тайминги
FIRST_REMINDER = 5 * 60   # 5 минут
SECOND_REMINDER = 5 * 60  # ещё 5 минут после первого
REPORT_TIMEOUT = 30 * 60  # 30 минут в отчёте
RESPONSE_TIMEOUT = 10 * 60  # 10 минут на ответ после вопроса "ты ещё в отчёте?"


# ------------------- Работа с очередью -------------------
def load_queue():
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_queue(queue):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


# ------------------- Напоминания -------------------
async def remind_to_take_report(chat_id, user_id, username):
    await asyncio.sleep(FIRST_REMINDER)
    queue = load_queue()

    if not queue or queue[0]["id"] != user_id or queue[0]["status"] != "waiting":
        return

    await bot.send_message(
        chat_id,
        f"@{username}, твоя очередь! Если не нажмешь /takereport в течение 5 минут, я буду вынужден удалить тебя из очереди😔"
    )

    await asyncio.sleep(SECOND_REMINDER)
    queue = load_queue()
    if not queue or queue[0]["id"] != user_id or queue[0]["status"] != "waiting":
        return

    # Удаляем из очереди и зовём следующего
    queue.pop(0)
    save_queue(queue)
    await bot.send_message(
        chat_id,
        f"@{username}, я устал ждать тебя и удалил из очереди 🫣. Если захочешь вернуться, нажми /standup"
    )

    if queue:
        next_user = queue[0]["username"]
        await bot.send_message(
            chat_id,
            f"🔥 @{next_user}, твоя очередь! Когда зайдешь в отчет, нажми /takereport"
        )


async def remind_user_in_report(chat_id, user_id, username):
    await asyncio.sleep(REPORT_TIMEOUT)
    queue = load_queue()

    if not queue or queue[0]["id"] != user_id or queue[0]["status"] != "in_progress":
        return

    await bot.send_message(chat_id, f"@{username}, ты еще в отчете? Если да, нажми /da, если нет, нажми /no")

    queue[0]["awaiting_response"] = True
    save_queue(queue)

    await asyncio.sleep(RESPONSE_TIMEOUT)
    queue = load_queue()

    if queue and queue[0]["id"] == user_id and queue[0].get("awaiting_response"):
        # Удаляем из очереди без ответа
        queue.pop(0)
        save_queue(queue)
        await bot.send_message(
            chat_id,
            f"@{username}, я не дождался твоего ответа и удалил тебя из очереди🫣. "
            f"Если захочешь вернуться, нажми /standup"
        )
        if queue:
            next_user = queue[0]["username"]
            await bot.send_message(
                chat_id,
                f"🔥 @{next_user}, твоя очередь! Когда зайдешь в отчет, нажми /takereport"
            )


# ------------------- Команды -------------------
@dp.message(Command("standup"))
async def cmd_standup(message: types.Message):
    queue = load_queue()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    if any(u["id"] == user_id for u in queue):
        await message.reply("Ты уже в очереди :)")
        return

    queue.append({"id": user_id, "username": username, "status": "waiting"})
    save_queue(queue)

    position = len(queue)
    await message.reply(
        f"Добавил тебя в очередь, твоя позиция {position}. Сейчас в очереди {len(queue)} человек(а)."
    )

    if len(queue) == 1:
        await bot.send_message(
            message.chat.id,
            f"@{username}, твоя очередь! Когда зайдешь в отчет, нажми /takereport"
        )
        asyncio.create_task(remind_to_take_report(message.chat.id, user_id, username))


@dp.message(Command("takereport"))
async def cmd_takereport(message: types.Message):
    queue = load_queue()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    if not queue:
        await message.reply("Очередь пустая. Чтобы встать в очередь нажми /standup.")
        return

    if queue[0]["id"] != user_id:
        await message.reply("Ну куда ты, пока не твоя очередь, подожди чуть-чуть 😅")
        return

    # Если пользователь пришёл после напоминаний — приветствуем :)
    if queue[0]["status"] == "waiting":
        await message.reply("Слава богу ты пришел(ла) 😂")

    queue[0]["status"] = "in_progress"
    queue[0]["awaiting_response"] = False
    save_queue(queue)

    await message.reply("Ты взял(а) отчет. Когда закончишь, нажми /finished")
    asyncio.create_task(remind_user_in_report(message.chat.id, user_id, username))


@dp.message(Command("finished"))
async def cmd_finished(message: types.Message):
    queue = load_queue()
    user_id = message.from_user.id

    if not queue:
        await message.reply("Очередь пустая. Чтобы встать, нажми /standup.")
        return

    if queue[0]["id"] != user_id:
        await message.reply("Понимаю, что не терпится, но ты не первый в очереди. Погоди немного 😁")
        return

    queue.pop(0)
    save_queue(queue)

    if not queue:
        await bot.send_message(
            message.chat.id,
            "В очереди никого нет, и я скучаю 😢 Нажми /standup, чтобы встать в очередь."
        )
    else:
        next_user = queue[0]["username"]
        await bot.send_message(
            message.chat.id,
            f"🔥 @{next_user}, твоя очередь! Когда зайдешь в отчет, нажми /takereport"
        )


@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    queue = load_queue()
    if not queue:
        await message.reply("Очередь пустая. Чтобы встать, нажми /standup.")
        return

    text = "Текущая очередь:\n"
    for i, u in enumerate(queue, start=1):
        status = " (в отчёте)" if u["status"] == "in_progress" else ""
        text += f"{i}. @{u['username']}{status}\n"
    await message.reply(text)


@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    queue = load_queue()
    user_id = message.from_user.id

    if not queue or not any(u["id"] == user_id for u in queue):
        await message.reply("Тебя нет в очереди 😳")
        return

    if queue[0]["id"] == user_id:
        await message.reply(
            "Ты не можешь себя удалить из очереди, так как сейчас твоя очередь. "
            "Сначала нажми /takereport и потом /finished, так ты отдашь очередь следующему человеку."
        )
        return

    queue = [u for u in queue if u["id"] != user_id]
    save_queue(queue)
    await message.reply("Ну вот блин, потеряли бойца 😅 Если захочешь вернуться, нажми /standup")


# ------------------- Реакции на /da и /no -------------------
@dp.message(Command("da"))
async def cmd_da(message: types.Message):
    queue = load_queue()
    if not queue:
        return

    user_id = message.from_user.id
    if queue[0]["id"] == user_id and queue[0].get("awaiting_response"):
        queue[0]["awaiting_response"] = False
        save_queue(queue)
        await message.reply("Хорошо, когда закончишь правки, нажми /finished")
        asyncio.create_task(remind_user_in_report(message.chat.id, user_id, queue[0]["username"]))


@dp.message(Command("no"))
async def cmd_no(message: types.Message):
    queue = load_queue()
    if not queue:
        return

    user_id = message.from_user.id
    if queue[0]["id"] == user_id:
        username = queue[0]["username"]
        queue.pop(0)
        save_queue(queue)
        await message.reply("Так, так, а мы тут все ждём тебя😭 Ладно, спасибо, передаю очередь другому.")
        if queue:
            next_user = queue[0]["username"]
            await bot.send_message(
                message.chat.id,
                f"🔥 @{next_user}, твоя очередь! Когда зайдешь в отчет, нажми /takereport"
            )


# ------------------- Запуск -------------------
if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
