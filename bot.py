import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ---------------- CONFIG ----------------
TOKEN = "8246901324:AAH3FHDKTJpVwPi66aZGU1PBv6R22WxPQL0"
QUEUE_FILE = "queue.json"

# Тайминги (в секундах)
FIRST_REMINDER = 5 * 60      # 5 минут
SECOND_REMINDER = 5 * 60     # ещё 5 минут после первого (итого 10)
REPORT_TIMEOUT = 30 * 60     # 30 минут в отчёте
REPORT_REPEAT_DELAY = 20 * 60  # 20 минут до повторного вопроса
REPORT_FINAL_WAIT = 10 * 60   # 10 минут после повторного вопроса -> удаление

# ----------------------------------------

bot = Bot(token=TOKEN)
dp = Dispatcher()

# pending tasks per chat-topic key
# structure: pending[key] = { "pre_take": {user_id: task}, "in_report": {user_id: task}, "repeat": {user_id: task} }
_pending: Dict[str, Dict[str, Dict[int, asyncio.Task]]] = {}

# ---------------- Storage helpers ----------------
def _chat_key(chat_id: int, thread_id: Optional[int]) -> str:
    tid = thread_id if thread_id is not None else 0
    return f"{chat_id}_{tid}"

def load_all() -> Dict[str, List[Dict[str, Any]]]:
    if not os.path.exists(QUEUE_FILE):
        return {}
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}

def save_all(data: Dict[str, List[Dict[str, Any]]]):
    with open(QUEUE_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(QUEUE_FILE + ".tmp", QUEUE_FILE)

def load_queue(key: str) -> List[Dict[str, Any]]:
    allq = load_all()
    return allq.get(key, [])

def save_queue(key: str, queue: List[Dict[str, Any]]):
    allq = load_all()
    allq[key] = queue
    save_all(allq)

def _ensure_pending(key: str):
    if key not in _pending:
        _pending[key] = {"pre_take": {}, "in_report": {}, "repeat": {}}

def _cancel_task(key: str, bucket: str, user_id: int):
    _ensure_pending(key)
    t = _pending[key].get(bucket, {}).get(user_id)
    if t and not t.done():
        t.cancel()
    if user_id in _pending[key].get(bucket, {}):
        _pending[key][bucket].pop(user_id, None)

# ---------------- Core helpers ----------------
def _find_index(queue: List[Dict[str, Any]], user_id: int) -> Optional[int]:
    for i, e in enumerate(queue):
        if int(e.get("id")) == int(user_id):
            return i
    return None

def _mention(u: Dict[str, Any]) -> str:
    username = u.get("username")
    if username:
        return f"@{username}"
    return u.get("first_name", "Пользователь")

async def _tag_next_and_schedule(chat_id: int, thread_id: Optional[int], key: str):
    """
    Send tag to next in queue and schedule pre_take timers for that user only.
    """
    queue = load_queue(key)
    if not queue:
        return
    next_entry = queue[0]
    username = next_entry.get("username") or next_entry.get("first_name", "")
    # send tag in the same thread (message_thread_id) as plain message (not reply)
    try:
        await bot.send_message(chat_id, f"@{username}, твоя очередь! Когда зайдешь в отчет, нажми /takereport", message_thread_id=thread_id)
    except Exception:
        # best-effort: ignore
        pass

    # schedule pre-take (5+5) for this user
    await _schedule_pre_take(chat_id, thread_id, key, next_entry)

# ---------------- Timers ----------------
async def _schedule_pre_take(chat_id: int, thread_id: Optional[int], key: str, user_entry: Dict[str, Any]):
    """
    For the user that was just tagged by bot: start 5min -> warning -> 5min -> remove if no /takereport.
    This runs only for the exact user who was tagged.
    """
    _ensure_pending(key)
    user_id = int(user_entry["id"])
    # cancel existing pre_take for user
    _cancel_task(key, "pre_take", user_id)

    async def seq():
        try:
            await asyncio.sleep(FIRST_REMINDER)
            queue = load_queue(key)
            # check: still exists AND is first AND status waiting
            if not queue or _find_index(queue, user_id) != 0 or queue[0].get("status") != "waiting":
                return
            # send warning (as a separate message in thread)
            username = queue[0].get("username") or queue[0].get("first_name", "")
            warn_text = f"@{username}, твоя очередь! Если не нажмешь /takereport в течение 5 минут, я буду вынужден удалить тебя из очереди😔."
            try:
                await bot.send_message(chat_id, warn_text, message_thread_id=thread_id)
            except Exception:
                pass
            # mark that we've warned (so if they /takereport after this we can reply "Слава богу..." text)
            queue[0]["warned_pre_take"] = True
            save_queue(key, queue)

            # second wait
            await asyncio.sleep(SECOND_REMINDER)
            queue = load_queue(key)
            if not queue or _find_index(queue, user_id) != 0 or queue[0].get("status") != "waiting":
                return
            # remove user
            removed = queue.pop(0)
            save_queue(key, queue)
            usrname = removed.get("username") or removed.get("first_name", "")
            try:
                await bot.send_message(chat_id, f"@{usrname}, я устал ждать тебя и удалил из очереди 🫣, простиии. Если захочешь вернуться, нажми /standup", message_thread_id=thread_id)
            except Exception:
                pass
            # schedule/tag next
            if queue:
                await _tag_next_and_schedule(chat_id, thread_id, key)
        except asyncio.CancelledError:
            return
        except Exception:
            return
        finally:
            _cancel_task(key, "pre_take", user_id)

    task = asyncio.create_task(seq())
    _pending[key]["pre_take"][user_id] = task

async def _schedule_in_report(chat_id: int, thread_id: Optional[int], key: str, user_entry: Dict[str, Any]):
    """
    In-report cycle: after /takereport -> 30m -> ask (reply to takereport_msg_id if present),
    then if no reaction -> 20m -> ask again, then 10m -> remove.
    We store per-entry fields: 'takereport_msg_id' (to reply to), 'awaiting_response' flag.
    """
    _ensure_pending(key)
    user_id = int(user_entry["id"])
    _cancel_task(key, "in_report", user_id)
    _cancel_task(key, "repeat", user_id)

    async def seq_main():
        try:
            # 30 minutes initial
            await asyncio.sleep(REPORT_TIMEOUT)
            queue = load_queue(key)
            if not queue or _find_index(queue, user_id) != 0 or queue[0].get("status") != "in_report":
                return

            # ask (reply to takereport_msg_id if exists)
            takemsg = queue[0].get("takereport_msg_id")
            username = queue[0].get("username") or queue[0].get("first_name", "")
            ask_text = f"@{username}, ты еще в отчете? Если да, нажми /da, если нет, нажми /no"
            try:
                if takemsg:
                    await bot.send_message(chat_id, ask_text, reply_to_message_id=takemsg, message_thread_id=thread_id)
                else:
                    await bot.send_message(chat_id, ask_text, message_thread_id=thread_id)
            except Exception:
                pass

            queue[0]["awaiting_response"] = True
            save_queue(key, queue)

            # wait 20 minutes for repeat
            await asyncio.sleep(REPORT_REPEAT_DELAY)
            queue2 = load_queue(key)
            if not queue2 or _find_index(queue2, user_id) != 0 or queue2[0].get("status") != "in_report" or not queue2[0].get("awaiting_response"):
                return

            # ask again
            try:
                if takemsg:
                    await bot.send_message(chat_id, ask_text, reply_to_message_id=takemsg, message_thread_id=thread_id)
                else:
                    await bot.send_message(chat_id, ask_text, message_thread_id=thread_id)
            except Exception:
                pass

            # wait final 10 minutes
            await asyncio.sleep(REPORT_FINAL_WAIT)
            queue3 = load_queue(key)
            if queue3 and _find_index(queue3, user_id) == 0 and queue3[0].get("status") == "in_report" and queue3[0].get("awaiting_response"):
                # remove and notify
                removed = queue3.pop(0)
                save_queue(key, queue3)
                usrname = removed.get("username") or removed.get("first_name", "")
                try:
                    await bot.send_message(chat_id, f"@{usrname}, я устал ждать тебя и удалил из очереди 🫣, простиии. Если захочешь вернуться, нажми /standup", message_thread_id=thread_id)
                except Exception:
                    pass
                if queue3:
                    await _tag_next_and_schedule(chat_id, thread_id, key)
            return
        except asyncio.CancelledError:
            return
        except Exception:
            return
        finally:
            _cancel_task(key, "in_report", user_id)
            _cancel_task(key, "repeat", user_id)

    task = asyncio.create_task(seq_main())
    _pending[key]["in_report"][user_id] = task

# ---------------- Handlers (commands) ----------------

@dp.message(Command("standup"))
async def cmd_standup(message: types.Message):
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)
    username = user.username or user.first_name or "Пользователь"

    queue = load_queue(key)
    if _find_index(queue, uid) is not None:
        await message.reply("Ты уже в очереди 👍")
        return

    entry = {
        "id": uid,
        "username": username,
        "first_name": user.first_name or "",
        "status": "waiting",  # waiting | in_report
        "warned_pre_take": False,
        "awaiting_response": False,
        "takereport_msg_id": None
    }
    queue.append(entry)
    save_queue(key, queue)

    pos = len(queue)
    await message.reply(f"Добавил тебя в очередь, твоя позиция {pos}. Сейчас в очереди {len(queue)} человек(а).")

    # If became first -> tag + schedule pre_take
    if pos == 1:
        # send tag (separate message in same thread)
        try:
            await bot.send_message(chat.id, f"@{username}, твоя очередь! Когда зайдешь в отчет, нажми /takereport", message_thread_id=thread_id)
        except Exception:
            # fallback reply
            await message.reply(f"@{username}, твоя очередь! Когда зайдешь в отчет, нажми /takereport")
        # schedule pre_take for this first
        await _schedule_pre_take(chat.id, thread_id, key, entry)

@dp.message(Command("takereport"))
async def cmd_takereport(message: types.Message):
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    queue = load_queue(key)
    if not queue:
        await message.reply("Очередь пустая. Чтобы встать в очередь, нажми /standup.")
        return

    idx = _find_index(queue, uid)
    if idx is None:
        await message.reply("Пока ты не в очереди. Чтобы встать нажми /standup")
        return

    if idx != 0:
        await message.reply("Ну куда ты, пока не твоя очередь 🙂 Я напишу, когда подойдет твой момент.")
        return

    # If was warned by pre_take process, cancel pre_take and show "Слава богу..." message
    if queue[0].get("warned_pre_take"):
        # cancel pre_take task
        _cancel_task(key, "pre_take", uid)
        # reply with the fun message first (as requested) then the standard takereport reply
        await message.reply("Слава богу ты пришел(ла) 😂")
        # ensure flag reset
        queue[0]["warned_pre_take"] = False

    # mark in_report and keep in queue
    queue[0]["status"] = "in_report"
    queue[0]["awaiting_response"] = False
    # store the message id of the user's /takereport so we can reply to it later
    queue[0]["takereport_msg_id"] = message.message_id
    save_queue(key, queue)

    await message.reply("Ты взял(а) отчет. Когда закончишь, нажми /finished")

    # cancel any pre_take pending for this user (if any)
    _cancel_task(key, "pre_take", uid)
    # schedule in-report reminders
    await _schedule_in_report(chat.id, thread_id, key, queue[0])

@dp.message(Command("finished"))
async def cmd_finished(message: types.Message):
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    queue = load_queue(key)
    if not queue:
        await message.reply("Очередь пустая. Чтобы встать в очередь нажми /standup")
        return

    idx = _find_index(queue, uid)
    if idx is None:
        await message.reply("Тебя нет в очереди. Чтобы встать в очередь нажми /standup")
        return

    if idx != 0:
        await message.reply("Ну куда ты, пока не твоя очередь, подожди чуть-чуть 😅")
        return

    if queue[0].get("status") != "in_report":
        await message.reply("Сначала зайди в отчет через /takereport, затем используй /finished.")
        return

    # remove first
    finished = queue.pop(0)
    save_queue(key, queue)

    # cancel any pending for this user
    _cancel_task(key, "in_report", uid)
    _cancel_task(key, "pre_take", uid)
    _cancel_task(key, "repeat", uid)

    # notify next or send "I'm lonely" message (as a reply to user's /finished)
    if queue:
        next_user = queue[0]
        try:
            await bot.send_message(chat.id, f"🔥 @{next_user.get('username')}, твоя очередь! Когда зайдешь в отчет, нажми /takereport", message_thread_id=thread_id)
        except Exception:
            await message.reply(f"🔥 @{next_user.get('username')}, твоя очередь! Когда зайдешь в отчет, нажми /takereport")
        # schedule pre_take for the new first
        await _schedule_pre_take(chat.id, thread_id, key, queue[0])
    else:
        # special friendly message when last user finished (separate message, not reply)
        try:
            await bot.send_message(chat.id, "В очереди никого нет, и я скучаю 😢 Нажми /standup, чтобы встать в очередь.", message_thread_id=thread_id)
        except Exception:
            await message.reply("В очереди никого нет, и я скучаю 😢 Нажми /standup, чтобы встать в очередь.")

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    queue = load_queue(key)
    idx = _find_index(queue, uid)
    if idx is None:
        await message.reply("Тебя нет в очереди 😉")
        return

    if idx == 0:
        # special message telling how to skip
        await message.reply("Ты не можешь себя удалить из очереди, так как сейчас твоя очередь. Чтобы пропустить, нажми /takereport, и далее используй /finished")
        return

    # remove and cancel any timers for that user
    removed = queue.pop(idx)
    save_queue(key, queue)
    _cancel_task(key, "pre_take", uid)
    _cancel_task(key, "in_report", uid)
    _cancel_task(key, "repeat", uid)

    await message.reply("Ну вот блин, потеряли бойца😅. Если захочешь вернуться, жми /standup")

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    queue = load_queue(key)
    if not queue:
        await message.reply("Сейчас никого нет в очереди. Нажми /standup, чтобы начать.")
        return

    lines = []
    for i, e in enumerate(queue, start=1):
        disp = f"@{e.get('username')}" if e.get("username") else e.get("first_name", "Пользователь")
        if i == 1 and e.get("status") == "in_report":
            lines.append(f"{i}) {disp} (в отчете)")
        else:
            lines.append(f"{i}) {disp}")
    await message.reply("\n".join(lines))

# reactions to /da and /no (must reply)
@dp.message(Command("da"))
async def cmd_da(message: types.Message):
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    queue = load_queue(key)
    if not queue:
        return

    if _find_index(queue, uid) != 0:
        return

    if not queue[0].get("awaiting_response"):
        return

    # user confirmed still in report
    queue[0]["awaiting_response"] = False
    save_queue(key, queue)

    # cancel in_report pending and reschedule another full 30-min cycle
    _cancel_task(key, "in_report", uid)
    await message.reply("Хорошо, когда закончишь правки, нажми /finished")
    await _schedule_in_report(chat.id, thread_id, key, queue[0])

@dp.message(Command("no"))
async def cmd_no(message: types.Message):
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    queue = load_queue(key)
    if not queue:
        return

    if _find_index(queue, uid) != 0:
        return

    # remove user and notify
    removed = queue.pop(0)
    save_queue(key, queue)
    # cancel pending tasks
    _cancel_task(key, "in_report", uid)
    _cancel_task(key, "pre_take", uid)
    _cancel_task(key, "repeat", uid)

    await message.reply("Так, так, а мы тут все ждем тебя😭 Ладно хоть не потерялсся в строчках отчета😅. Передаю очередь другому.")
    # tag next if exists
    if queue:
        await _tag_next_and_schedule(chat.id, thread_id, key)

# ---------------- Startup / Shutdown ----------------
async def _cancel_all():
    for key, buckets in list(_pending.items()):
        for bucket in buckets.values():
            for t in list(bucket.values()):
                if t and not t.done():
                    t.cancel()
    _pending.clear()

async def main():
    # ensure file exists
    if not os.path.exists(QUEUE_FILE):
        save_all({})
    try:
        await dp.start_polling(bot)
    finally:
        await _cancel_all()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))

