# bot.py — полный рабочий код с функциями: standup, takereport, finished, delete, list,
# da, no, skip, fastreport (+ timers 5+5, 30+20+10, fastrequest 2 minutes), per-thread queues
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
FIRST_REMINDER = 5 * 60        # 5 минут до предупреждения (pre-take)
SECOND_REMINDER = 5 * 60       # ещё 5 минут до удаления (pre-take)
REPORT_TIMEOUT = 30 * 60       # 30 минут в отчёте (первичный)
REPORT_REPEAT_DELAY = 20 * 60  # 20 минут до повторного вопроса
REPORT_FINAL_WAIT = 10 * 60    # 10 минут после повторного вопроса -> удаление
FASTREQUEST_TIMEOUT = 2 * 60   # 2 минуты для ответа на /fastreport от current
# ----------------------------------------

bot = Bot(token=TOKEN)
dp = Dispatcher()

# pending tasks per chat-topic key
# structure: _pending[key] = {"pre_take": {user_id: task}, "in_report": {user_id: task}, "fastreq": {user_id: task}}
_pending: Dict[str, Dict[str, Dict[int, asyncio.Task]]] = {}

# ---------------- Storage helpers ----------------
def _chat_key(chat_id: int, thread_id: Optional[int]) -> str:
    tid = thread_id if thread_id is not None else 0
    return f"{chat_id}_{tid}"

def _ensure_storage_file():
    if not os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

def load_all() -> Dict[str, Dict[str, Any]]:
    _ensure_storage_file()
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}

def save_all(data: Dict[str, Dict[str, Any]]):
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE_FILE)

def load_state(key: str) -> Dict[str, Any]:
    all_data = load_all()
    return all_data.get(key, {"queue": [], "paused": None, "fast_request": None})

def save_state(key: str, state: Dict[str, Any]):
    all_data = load_all()
    all_data[key] = state
    save_all(all_data)

def load_queue(key: str) -> List[Dict[str, Any]]:
    return load_state(key).get("queue", [])

def save_queue(key: str, queue: List[Dict[str, Any]]):
    state = load_state(key)
    state["queue"] = queue
    save_state(key, state)

# paused: stored as an entry dict or None
def load_paused(key: str) -> Optional[Dict[str, Any]]:
    return load_state(key).get("paused")

def save_paused(key: str, paused_entry: Optional[Dict[str, Any]]):
    state = load_state(key)
    state["paused"] = paused_entry
    save_state(key, state)

# fast_request: dict like {"new_user": {...}, "task_user_id": id} or None
def load_fastrequest(key: str) -> Optional[Dict[str, Any]]:
    return load_state(key).get("fast_request")

def save_fastrequest(key: str, fr: Optional[Dict[str, Any]]):
    state = load_state(key)
    state["fast_request"] = fr
    save_state(key, state)

# ---------------- Pending tasks management ----------------
def _ensure_pending(key: str):
    if key not in _pending:
        _pending[key] = {"pre_take": {}, "in_report": {}, "fastreq": {}}

def _cancel_task(key: str, bucket: str, user_id: int):
    _ensure_pending(key)
    t = _pending[key].get(bucket, {}).get(user_id)
    if t and not t.done():
        t.cancel()
    if user_id in _pending[key].get(bucket, {}):
        _pending[key][bucket].pop(user_id, None)

def _cancel_all_for_user(key: str, user_id: int):
    for bucket in ("pre_take", "in_report", "fastreq"):
        _cancel_task(key, bucket, user_id)

# ---------------- Helpers ----------------
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

# ---------------- Core: tag next and schedule pre_take ----------------
async def _tag_next_and_schedule(chat_id: int, thread_id: Optional[int], key: str):
    queue = load_queue(key)
    if not queue:
        return
    next_entry = queue[0]
    username = next_entry.get("username") or next_entry.get("first_name", "")
    # send tag message in the same thread (not as reply)
    try:
        await bot.send_message(chat_id, f"🔥 @{username}, твоя очередь! Когда зайдешь в отчет, нажми /takereport. Чтобы пропустить, нажми /skip", message_thread_id=thread_id)
    except Exception:
        # ignore
        pass
    # schedule pre-take only for this tagged user
    await _schedule_pre_take(chat_id, thread_id, key, next_entry)

# ---------------- Timers ----------------
async def _schedule_pre_take(chat_id: int, thread_id: Optional[int], key: str, user_entry: Dict[str, Any]):
    """
    5min -> warning -> 5min -> remove if no /takereport.
    This timer is only relevant for the user who was tagged.
    """
    _ensure_pending(key)
    user_id = int(user_entry["id"])
    _cancel_task(key, "pre_take", user_id)

    async def seq():
        try:
            await asyncio.sleep(FIRST_REMINDER)
            queue = load_queue(key)
            if not queue or _find_index(queue, user_id) != 0 or queue[0].get("status") != "waiting":
                return
            # warn
            username = queue[0].get("username") or queue[0].get("first_name", "")
            warn_text = f"@{username}, твоя очередь! Если не нажмешь /takereport в течение 5 минут, я буду вынужден удалить тебя из очереди😔. Чтобы пропустить, нажми /skip. "
            try:
                await bot.send_message(chat_id, warn_text, message_thread_id=thread_id)
            except Exception:
                pass
            # mark warned
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
            # schedule next
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
    In-report cycle: 30m -> ask (reply to takereport_msg_id if present) -> wait 20m -> ask again -> wait 10m -> remove.
    """
    _ensure_pending(key)
    user_id = int(user_entry["id"])
    # cancel existing
    _cancel_task(key, "in_report", user_id)
    _cancel_task(key, "fastreq", user_id)

    async def seq():
        try:
            # wait 30 minutes
            await asyncio.sleep(REPORT_TIMEOUT)
            queue = load_queue(key)
            if not queue or _find_index(queue, user_id) != 0 or queue[0].get("status") != "in_report":
                return
            takemsg = queue[0].get("takereport_msg_id")
            username = queue[0].get("username") or queue[0].get("first_name", "")
            ask_text = f"@{username}, ты ещё в отчете? Если да, нажми /da, если нет, нажми /no"
            try:
                if takemsg:
                    await bot.send_message(chat_id, ask_text, reply_to_message_id=takemsg, message_thread_id=thread_id)
                else:
                    await bot.send_message(chat_id, ask_text, message_thread_id=thread_id)
            except Exception:
                pass
            queue[0]["awaiting_response"] = True
            save_queue(key, queue)
            # wait REPORT_REPEAT_DELAY
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
            # wait final
            await asyncio.sleep(REPORT_FINAL_WAIT)
            queue3 = load_queue(key)
            if queue3 and _find_index(queue3, user_id) == 0 and queue3[0].get("status") == "in_report" and queue3[0].get("awaiting_response"):
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

    task = asyncio.create_task(seq())
    _pending[key]["in_report"][user_id] = task

# ---------------- Fastrequest handling ----------------
async def _schedule_fastrequest_timeout(chat_id: int, thread_id: Optional[int], key: str, current_id: int, new_user: Dict[str, Any]):
    """
    If current user doesn't reply within FASTREQUEST_TIMEOUT, inform new user and clear fast_request.
    """
    _ensure_pending(key)
    uid = int(current_id)
    _cancel_task(key, "fastreq", uid)

    async def seq():
        try:
            await asyncio.sleep(FASTREQUEST_TIMEOUT)
            fr = load_fastrequest(key)
            if not fr:
                return
            # check if still same fast request
            if fr.get("current_id") != current_id or fr.get("new_user", {}).get("id") != new_user.get("id"):
                return
            # timeout: tell new_user that current didn't answer
            try:
                await bot.send_message(chat_id, f"@{new_user.get('username')}, @{fr.get('current_username')} пока не ответил 😔 Попробуй чуть позже или дождись своей очереди.", message_thread_id=thread_id)
            except Exception:
                pass
            # clear fast_request
            save_fastrequest(key, None)
        except asyncio.CancelledError:
            return
        except Exception:
            return
        finally:
            _cancel_task(key, "fastreq", uid)

    task = asyncio.create_task(seq())
    _pending[key]["fastreq"][uid] = task

# ---------------- Handlers (commands) ----------------

@dp.message(Command("standup"))
async def cmd_standup(message: types.Message):
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)
    username = user.username or user.first_name or "Пользователь"

    state = load_state(key)
    queue = state.get("queue", [])

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
        try:
            await bot.send_message(chat.id, f"🔥 @{username}, твоя очередь! Когда зайдешь в отчет, нажми /takereport. Чтобы пропустить, нажми /skip", message_thread_id=thread_id)
        except Exception:
            await message.reply(f"🔥 @{username}, твоя очередь! Когда зайдешь в отчет, нажми /takereport. Чтобы пропустить, нажми /skip")
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

    # If was warned by pre_take process -> cancel and send fun text
    if queue[0].get("warned_pre_take"):
        _cancel_task(key, "pre_take", uid)
        await message.reply("Слава богу ты пришел(ла) 😂")
        queue[0]["warned_pre_take"] = False

    # mark in_report and keep in queue
    queue[0]["status"] = "in_report"
    queue[0]["awaiting_response"] = False
    # store message id for replies when asking about in-report
    queue[0]["takereport_msg_id"] = message.message_id
    save_queue(key, queue)

    await message.reply("Ты взял(а) отчет. Когда закончишь, нажми /finished")

    # cancel any pre_take pending for this user
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
        await message.reply("Ну не финишурай, а сначала встань в очередь. Чтобы встать в очередь нажми /standup")
        return

    if idx != 0:
        await message.reply("Ну куда ты выходишь, пока не твоя очередь, подожди чуть-чуть 😅")
        return

    if queue[0].get("status") != "in_report":
        await message.reply("Сначала зайди в отчет через /takereport, затем используй /finished.")
        return

    # remove first
    finished = queue.pop(0)
    save_queue(key, queue)

    # cancel pending for this user
    _cancel_all_for_user(key, uid)

    # handle paused restore logic:
    paused = load_paused(key)
    if paused:
        # when new finished, we should restore paused user to front
        # insert paused at front and clear paused
        q = load_queue(key)
        q.insert(0, paused)
        save_queue(key, q)
        save_paused(key, None)
        # notify paused that queue returned (reply to finished)
        try:
            await message.reply(f"@{paused.get('username')}, твоя очередь возвращена! Когда будешь готов(а), нажми /takereport")
        except Exception:
            # fallback: send separate
            try:
                await bot.send_message(chat.id, f"@{paused.get('username')}, твоя очередь возвращена! Когда будешь готов(а), нажми /takereport", message_thread_id=thread_id)
            except Exception:
                pass
        # do NOT tag next automatically because paused is now first and must press /takereport
        return

    # notify next or send "I'm lonely" message (as a reply to user's /finished)
    if queue:
        next_user = queue[0]
        try:
            await bot.send_message(chat.id, f"🔥 @{next_user.get('username')}, твоя очередь! Когда зайдешь в отчет, нажми /takereport. Чтобы пропустить, нажми /skip", message_thread_id=thread_id)
        except Exception:
            await message.reply(f"🔥 @{next_user.get('username')}, твоя очередь! Когда зайдешь в отчет, нажми /takereport. Чтобы пропустить, нажми /skip")
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
        await message.reply("Удалить себя не получится, потому что ты не в очереди😉")
        return

    if idx == 0:
        # special message telling how to skip
        await message.reply("Ты не можешь себя удалить из очереди, так как сейчас твоя очередь. Чтобы пропустить, нажми /skip")
        return

    # remove and cancel any timers for that user
    removed = queue.pop(idx)
    save_queue(key, queue)
    _cancel_all_for_user(key, uid)

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
        disp = f"@{e.get('username')}" if e.get('username') else e.get('first_name', "Пользователь")
        if i == 1 and e.get("status") == "in_report":
            lines.append(f"{i}) {disp} (в отчете)")
        else:
            lines.append(f"{i}) {disp}")
    await message.reply("\n".join(lines))

# ---------------- skip command ----------------
@dp.message(Command("skip"))
async def cmd_skip(message: types.Message):
    """
    /skip - доступна только если пользователь сейчас первый (и бот приглашал его).
    Перенос в конец очереди, отправка сообщения 'Принял, ...', вызов следующего.
    """
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    queue = load_queue(key)
    if not queue:
        await message.reply("Ну что ты тут скипаешь, тебя же нет в очереди. Двай не хулигань мне тут, а нажми /standup.")
        return

    idx = _find_index(queue, uid)
    if idx is None:
        await message.reply("Ну что ты тут скипаешь, тебя же нет в очереди. Двай не хулигань мне тут, а нажми /standup.")
        return

    if idx != 0:
        await message.reply("Команда /skip доступна только тогда, когда до тебя дошла очередь (ты первый).")
        return

    # move to end
    removed = queue.pop(0)
    queue.append(removed)
    save_queue(key, queue)

    # cancel pending for this user
    _cancel_all_for_user(key, uid)

    # reply confirming
    pos = len(queue)
    await message.reply(f"Принял, @{removed.get('username')}! Перенес тебя в позицию №{pos} 🏃‍♂️")

    # tag next
    if queue:
        await _tag_next_and_schedule(chat.id, thread_id, key)

# ---------------- fastreport (вне очереди вход) ----------------
@dp.message(Command("fastreport"))
async def cmd_fastreport(message: types.Message):
    """
    /fastreport - зайти вне очереди.
    Если никто в отчете, принять и стать первым in_report.
    Если кто-то в отчете, спросить у current: /yes or /no
    """
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    new_user = message.from_user
    new_id = int(new_user.id)
    new_username = new_user.username or new_user.first_name or "Пользователь"

    queue = load_queue(key)
    current = queue[0] if queue else None

    # if no one in report (either queue empty or first not in_report)
    if not queue or (queue and queue[0].get("status") != "in_report"):
        # put new_user to front and mark in_report
        # remove if already in queue somewhere
        if _find_index(queue, new_id) is not None:
            # remove existing entry
            idx = _find_index(queue, new_id)
            queue.pop(idx)
        entry = {
            "id": new_id,
            "username": new_username,
            "first_name": new_user.first_name or "",
            "status": "in_report",
            "warned_pre_take": False,
            "awaiting_response": False,
            "takereport_msg_id": message.message_id
        }
        queue.insert(0, entry)
        save_queue(key, queue)
        await message.reply(f"@{new_username}, отчет свободен, заходи! Когда закончишь, нажми /finished")
        # schedule in-report timers for this new first
        await _schedule_in_report(chat.id, thread_id, key, entry)
        return

    # someone is already in_report -> ask them
    cur_id = int(current.get("id"))
    cur_username = current.get("username") or current.get("first_name", "")
    # store fast_request
    fr = {"current_id": cur_id, "current_username": cur_username,
          "new_user": {"id": new_id, "username": new_username, "first_name": new_user.first_name or ""}}
    save_fastrequest(key, fr)

    # ask current (reply to new user's message)
    try:
        await bot.send_message(chat.id, f"@{cur_username}, @{new_username} хочет зайти вне очереди. Пропустишь его(ее)? Нажми /yes или /no", message_thread_id=thread_id)
    except Exception:
        await message.reply(f"@{cur_username}, @{new_username} хочет зайти вне очереди. Пропустишь его(ее)? Нажми /yes или /no")

    # schedule timeout for current reply
    await _schedule_fastrequest_timeout(chat.id, thread_id, key, cur_id, fr["new_user"])

# ---------------- yes/no handlers for fastrequest and in-report -> reuse /da and /no for in-report
@dp.message(Command("yes"))
async def cmd_yes(message: types.Message):
    """
    Used by current user to accept fastreport request.
    """
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    fr = load_fastrequest(key)
    if not fr:
        # nothing pending
        await message.reply("Нет запросов на внеочередной вход.")
        return

    if int(fr.get("current_id")) != uid:
        await message.reply("Ты не тот, у кого спрашивают разрешение.")
        return

    # accept: pause current user, allow new_user to be in_report now
    queue = load_queue(key)
    if not queue or int(queue[0].get("id")) != uid:
        # current changed — cancel
        save_fastrequest(key, None)
        await message.reply("Похоже, сейчас ты не в отчете, отменяю запрос.")
        return

    new_user = fr.get("new_user")
    # remove current from queue and set paused
    paused_entry = queue.pop(0)
    paused_entry["status"] = "waiting"  # paused stored as waiting — will be restored front later
    save_paused(key, paused_entry)
    save_queue(key, queue)

    # cancel current's in_report tasks
    _cancel_all_for_user(key, uid)

    # insert new_user as in_report first (remove existing occurrence if any)
    q = load_queue(key)
    # remove any existing entry for new_user in queue
    existing_index = _find_index(q, int(new_user.get("id")))
    if existing_index is not None:
        q.pop(existing_index)
    new_entry = {
        "id": int(new_user.get("id")),
        "username": new_user.get("username"),
        "first_name": new_user.get("first_name", ""),
        "status": "in_report",
        "warned_pre_take": False,
        "awaiting_response": False,
        "takereport_msg_id": None
    }
    q.insert(0, new_entry)
    save_queue(key, q)
    # inform both
    await message.reply(f"@{new_user.get('username')}, добро пожаловать вне очереди 🚀")
    try:
        await bot.send_message(chat.id, f"@{paused_entry.get('username')}, я тебя временно поставил на паузу, верну отчет, как только @{new_user.get('username')} закончит.", message_thread_id=thread_id)
    except Exception:
        pass
    # schedule in-report for the new first
    await _schedule_in_report(chat.id, thread_id, key, new_entry)
    # clear fast_request and cancel its timeout
    save_fastrequest(key, None)
    _cancel_task(key, "fastreq", uid)

@dp.message(Command("no"))
async def cmd_no(message: types.Message):
    """
    Overloaded: used both for in-report user saying they are not in report (handled earlier)
    and for fastrequest denial (if deny by current for fastreport).
    For fastrequest: if current denies, we inform new_user and clear fast_request.
    For in-report scenario, /no handling was implemented earlier as reaction to "are you still in report?".
    We'll detect context by whether there's a pending fast_request and by who called.
    """
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)
    user = message.from_user
    uid = int(user.id)

    fr = load_fastrequest(key)
    if fr and int(fr.get("current_id")) == uid:
        # current denied fastreport
        new = fr.get("new_user")
        try:
            await bot.send_message(chat.id, f"@{new.get('username')}, @ {fr.get('current_username')} пока не готов тебя пропустить 😅 Подожди своей очереди, пожалуйста.", message_thread_id=thread_id)
        except Exception:
            try:
                await message.reply(f"@{new.get('username')}, @{fr.get('current_username')} пока не готов тебя пропустить 😅 Подожди своей очереди, пожалуйста.")
            except Exception:
                pass
        save_fastrequest(key, None)
        _cancel_task(key, "fastreq", uid)
        return

    # else: try to handle as in-report "no" (user says they are NOT in report)
    # This branch works if the /no was used after bot asked "ты еще в отчете?"
    queue = load_queue(key)
    if not queue:
        return
    if _find_index(queue, uid) != 0:
        return

    if not queue[0].get("awaiting_response"):
        return

    # remove user and notify
    username = queue[0].get("username")
    queue.pop(0)
    save_queue(key, queue)
    # cancel tasks
    _cancel_all_for_user(key, uid)
    await message.reply("Так, так, а мы тут все ждем тебя😭 Ладно, спасибо, передаю очередь другому. Если тут никого нет, то чтобы встать в очередь нажми /standup")
    if queue:
        await _tag_next_and_schedule(chat.id, thread_id, key)

# ---------------- da (user confirms still in report) ----------------
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

# ---------------- Startup / Shutdown ----------------
async def _cancel_all():
    for key, buckets in list(_pending.items()):
        for bucket in buckets.values():
            for t in list(bucket.values()):
                if t and not t.done():
                    t.cancel()
    _pending.clear()

async def main():
    _ensure_storage_file()
    try:
        await dp.start_polling(bot)
    finally:
        await _cancel_all()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))

