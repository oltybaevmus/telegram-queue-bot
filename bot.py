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
RESPONSE_TIMEOUT = 10 * 60  # 10 минут на ответ после "ты ещё в отчёте?"


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
async def remind_to_take_report(chat_id, message_thread_id, user_id, username):
    await asyncio.sleep(FIRST_REMINDER)
    queue = load_queue()

    if not queue or queue[0]["id"] != user_id or queue[0]["status"] != "waiting":
        return

    # Помечаем, что человеку уже напомнили
    queue[0]["reminded"] = True
    save_queue(queue)

    await bot.send_message(
        chat_id,
        f"@{username}, твоя очередь! Если не нажмешь /takereport в течение 5 минут, я буду вынужден удалить тебя из очереди😔.",
        message_thread_id=message_thread_id
    )

    await asyncio.sleep(SECOND_REMINDER)
    queue = load_queue()
    if not queue or queue[0]["id"] != user_id or queue[0]["status"] != "waiting":
        return

    queue.pop(0)
    save_queue(queue)
    await bot.send_message(
        chat_id,
        f"@{username}, я устал ждать тебя и удалил из очереди 🫣, простиии. Если захочешь вернуться, нажми /standup",
        message_thread_id=message_thread_id
    )

    if queue:
        next_user = queue[0]["username"]
        await bot.send_message(
            chat_id,
            f"🔥 @{next_user}, твоя очередь! Когда зайдешь в отчет, нажми /takereport",
            message_thread_id=message_thread_id
        )


async def remind_user_in_report(chat_id, message_thread_id, user_id, username):
    await asyncio.sleep(REPORT_TIMEOUT)
    queue = load_queue()

    if not queue or queue[0]["id"] != user_id or queue[0]["status"] != "in_progress":
        return

    await bot.send_message(
        chat_id,
        f"@{username}, ты еще в отчете? Если да, нажми /da, если нет, нажми /no",
        message_thread_id=message_thread_id
    )

    queue[0]["awaiting_response"] = True
    save_queue(queue)

    await asyncio.sleep(RESPONSE_TIMEOUT)
    queue = load_queue()

    if queue and queue[0]["id"] == user_id and queue[0].get("awaiting_response"):
        queue.pop(0)
        save_queue(queue)
        await bot.send_message(
            chat_id,
            f"@{username}, я не дождался твоего ответа и удалил тебя из очереди🫣, простиии. "
            f"Если захочешь вернуться, нажми /standup",
            message_thread_id=message_thread_id
        )
        if queue:
            next_user = queue[0]["username"]
            await bot.send_message(
                chat_id,
                f"🔥 @{next_user}, твоя очередь! Когда зайдешь в отчет, нажми /takereport",
                message_thread_id=message_thread_id
            )


# ------------------- Команды -------------------
@dp.message(Command("standup"))
async def cmd_standup(message: types.Message):
    queue = load_queue()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    message_thread_id = message.message_thread_id
    chat_id = message.chat.id

    if any(u["id"] == user_id for u in queue):
        await bot.send_message(chat_id, "Ты уже в очереди 👍", message_thread_id=message_thread_id)
        return

    queue.append({
        "id": user_id,
        "username": username,
        "status": "waiting",
        "reminded": False
    })
    save_queue(queue)

    position = len(queue)
    await bot.send_message(
        chat_id,
        f"Добавил тебя в очередь, твоя позиция {position}. Сейчас в очереди {len(queue)} человек(а).",
        message_thread_id=message_thread_id
    )

    if len(queue) == 1:
        await bot.send_message(
            chat_id,
            f"@{username}, твоя очередь! Когда зайдешь в отчет, нажми /takereport",
            message_thread_id=message_thread_id
        )
        asyncio.create_task(remind_to_take_report(chat_id, message_thread_id, user_id, username))


@dp.message(Command("takereport"))
async def cmd_takereport(message: types.Message):
    queue = load_queue()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    message_thread_id = message.message_thread_id
    chat_id = message.chat.id

    if not queue:
        await bot.send_message(chat_id, "Очередь пустая. Чтобы встать в очередь нажми /standup.", message_thread_id=message_thread_id)
        return

    if queue[0]["id"] != user_id:
        await bot.send_message(chat_id, "Ну куда ты, пока не твоя очередь, подожди чуть-чуть 😅", message_thread_id=message_thread_id)
        return

    # Показываем фразу "Слава богу ты пришёл" только если был reminder
    if queue[0].get("reminded"):
        await bot.send_message(chat_id, "Слава богу ты пришел(ла)😂", message_thread_id=message_thread_id)
        queue[0]["reminded"] = False

    queue[0]["status"] = "in_progress"
    queue[0]["awaiting_response"] = False
    save_queue(queue)

    await bot.send_message(chat_id, "Ты взял(а) отчет. Когда закончишь, нажми /finished", message_thread_id=message_thread_id)
    asyncio.create_task(remind_user_in_report(chat_id, message_thread_id, user_id, username))


@dp.message(Command("finished"))
async def cmd_finished(message: types.Message):
    queue = load_queue()
    user_id = message.from_user.id
    message_thread_id = message.message_thread_id
    chat_id = message.chat.id

    if not queue:
        await bot.send_message(chat_id, "Очередь пустая. Чтобы встать, нажми /standup.", message_thread_id=message_thread_id)
        return

    if queue[0]["id"] != user_id:
        await bot.send_message(chat_id, "Понимаю, что не терпится, но ты не первый в очереди. Погоди немного 😁", message_thread_id=message_thread_id)
        return

    queue.pop(0)
    save_queue(queue)

    if not queue:
        await bot.send_message(
            chat_id,
            "В очереди никого нет, и я скучаю 😢 Нажми /standup, чтобы встать в очередь.",
            message_thread_id=message_thread_id
        )
    else:
        next_user = queue[0]["username"]
        await bot.send_message(
            chat_id,
            f"🔥 @{next_user}, твоя очередь! Когда зайдешь в отчет, нажми /takereport",
            message_thread_id=message_thread_id
        )


@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    queue = load_queue()
    message_thread_id = message.message_thread_id
    chat_id = message.chat.id

    if not queue:
        await bot.send_message(chat_id, "Очередь пустая. Чтобы встать, нажми /standup.", message_thread_id=message_thread_id)
        return

    text = "Текущая очередь:\n"
    for i, u in enumerate(queue, start=1):
        status = " (в отчёте)" if u["status"] == "in_progress" else ""
        text += f"{i}. @{u['username']}{status}\n"
    await bot.send_message(chat_id, text, message_thread_id=message_thread_id)


@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    queue = load_queue()
    user_id = message.from_user.id
    message_thread_id = message.message_thread_id
    chat_id = message.chat.id

    if not queue or not any(u["id"] == user_id for u in queue):
        await bot.send_message(chat_id, "Тебя нет в очереди 😳", message_thread_id=message_thread_id)
        return

    if queue[0]["id"] == user_id:
        await bot.send_message(
            chat_id,
            "Ты не можешь себя удалить из очереди, так как сейчас твоя очеред. "
            "Сначала нажми /takereport и потом /finished, так ты отдашь очередь следующему участнику.",
            message_thread_id=message_thread_id
        )
        return

    queue = [u for u in queue if u["id"] != user_id]
    save_queue(queue)
    await bot.send_message(chat_id, "Ну вот блин, потеряли бойца 😅 Если захочешь вернуться, нажми /standup", message_thread_id=message_thread_id)


# ------------------- Реакции на /da и /no -------------------
@dp.message(Command("da"))
async def cmd_da(message: types.Message):
    queue = load_queue()
    message_thread_id = message.message_thread_id
    chat_id = message.chat.id

    if not queue:
        return

    user_id = message.from_user.id
    if queue[0]["id"] == user_id and queue[0].get("awaiting_response"):
        queue[0]["awaiting_response"] = False
        save_queue(queue)
        await bot.send_message(chat_id, "Хорошо, когда закончишь правки, нажми /finished", message_thread_id=message_thread_id)
        asyncio.create_task(remind_user_in_report(chat_id, message_thread_id, user_id, queue[0]["username"]))


@dp.message(Command("no"))
async def cmd_no(message: types.Message):
    queue = load_queue()
    message_thread_id = message.message_thread_id
    chat_id = message.chat.id

    if not queue:
        return

    user_id = message.from_user.id
    if queue[0]["id"] == user_id:
        username = queue[0]["username"]
        queue.pop(0)
        save_queue(queue)
        await bot.send_message(chat_id, "Так, так, а мы тут все ждём тебя😭 Ладно, спасибо, передаю очередь другому.", message_thread_id=message_thread_id)
        if queue:
            next_user = queue[0]["username"]
            await bot.send_message(
                chat_id,
                f"🔥 @{next_user}, твоя очередь! Когда зайдешь в отчет, нажми /takereport",
                message_thread_id=message_thread_id
            )


# ------------------- Запуск -------------------
if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))

