import os
import json
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Any

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode

# ---------------- CONFIG ----------------
# ВСТАВЬ СВОЙ ТОКЕН (тот, который ты предоставил)
TOKEN = "8246901324:AAH3FHDKTJpVwPi66aZGU1PBv6R22WxPQL0"

QUEUE_FILE = "queue.json"
# Тайминги (в секундах)
WARNING_DELAY = 5 * 60      # 5 минут до предупреждения
DELETION_DELAY = 5 * 60     # ещё 5 минут до удаления после предупреждения
# ----------------------------------------

bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# In-memory structures for timers and locks
# _pending_timers[key][user_id] = {"task": asyncio.Task, "warning_sent": bool}
_pending_timers: Dict[str, Dict[int, Dict[str, Any]]] = {}
_storage_lock = asyncio.Lock()
_chat_locks: Dict[str, asyncio.Lock] = {}

# ---------------- Helpers ----------------
def _chat_key(chat_id: int, thread_id: Optional[int]) -> str:
    tid = thread_id if thread_id is not None else 0
    return f"{chat_id}_{tid}"

def _get_chat_lock(key: str) -> asyncio.Lock:
    if key not in _chat_locks:
        _chat_locks[key] = asyncio.Lock()
    return _chat_locks[key]

def _find_index_by_user(queue: List[Dict], user_id: int) -> Optional[int]:
    for i, e in enumerate(queue):
        if int(e.get("user_id")) == int(user_id):
            return i
    return None

def _entry_display(e: Dict) -> str:
    username = e.get("username")
    first_name = e.get("first_name") or "Пользователь"
    return f"@{username}" if username else first_name

def _mention_html(e: Dict) -> str:
    if e.get("username"):
        return f"@{e['username']}"
    return f'<a href="tg://user?id={e["user_id"]}">{e.get("first_name") or "Пользователь"}</a>'

# ---------------- Storage ----------------
async def _ensure_storage_exists():
    async with _storage_lock:
        if not os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)

async def _read_storage() -> Dict[str, List[Dict]]:
    async with _storage_lock:
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            return {}
    return {}

async def _write_storage(data: Dict[str, List[Dict]]):
    async with _storage_lock:
        tmp = QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, QUEUE_FILE)

async def load_queue_for_key(key: str) -> List[Dict]:
    data = await _read_storage()
    return data.get(key, [])

async def save_queue_for_key(key: str, queue: List[Dict]):
    data = await _read_storage()
    data[key] = queue
    await _write_storage(data)

# ---------------- Pending timers management ----------------
def _ensure_pending_for(key: str):
    if key not in _pending_timers:
        _pending_timers[key] = {}

async def _cancel_pending_for_user(key: str, user_id: int):
    _ensure_pending_for(key)
    info = _pending_timers[key].get(user_id)
    if info:
        task = info.get("task")
        if task and not task.done():
            task.cancel()
    _pending_timers[key].pop(user_id, None)

async def _schedule_skip_sequence(chat_id: int, thread_id: Optional[int], key: str, user_entry: Dict):
    """
    Launch 5min warning + 5min deletion sequence for the given user (who was notified).
    """
    _ensure_pending_for(key)
    user_id = int(user_entry["user_id"])
    # cancel existing
    await _cancel_pending_for_user(key, user_id)

    async def sequence():
        try:
            # first wait
            await asyncio.sleep(WARNING_DELAY)
            async with _get_chat_lock(key):
                queue = await load_queue_for_key(key)
                if not queue or _find_index_by_user(queue, user_id) != 0:
                    return
                if queue[0].get("status") == "in_report":
                    return
                # send warning with tag (per choice A)
                mention = _mention_html(queue[0])
                warn_text = (f"{mention} твоя очередь, если ты не нажмешь /takereport, "
                             f"то через 5 минут я буду вынужден тебя удалить из очереди.")
                try:
                    await bot.send_message(chat_id, warn_text, parse_mode=ParseMode.HTML, message_thread_id=thread_id)
                except Exception:
                    pass
                # mark warning_sent
                if key not in _pending_timers:
                    _ensure_pending_for(key)
                if user_id in _pending_timers.get(key, {}):
                    _pending_timers[key][user_id]["warning_sent"] = True

            # second wait
            await asyncio.sleep(DELETION_DELAY)
            async with _get_chat_lock(key):
                queue = await load_queue_for_key(key)
                if not queue or _find_index_by_user(queue, user_id) != 0:
                    return
                if queue[0].get("status") == "in_report":
                    return
                # remove and announce removal
                removed = queue.pop(0)
                await save_queue_for_key(key, queue)
                mention_removed = _mention_html(removed)
                try:
                    await bot.send_message(chat_id, f"{mention_removed} Я устал ждать тебя и удалил из очереди. Теперь, чтобы вернуться в очередь нажми /standup", parse_mode=ParseMode.HTML, message_thread_id=thread_id)
                except Exception:
                    pass
                # notify next and schedule for them
                if queue:
                    next_entry = queue[0]
                    next_mention = _mention_html(next_entry)
                    notify_text = f"{next_mention} Твоя очередь. Когда зайдешь в отчет, нажми /takereport"
                    try:
                        await bot.send_message(chat_id, notify_text, parse_mode=ParseMode.HTML, message_thread_id=thread_id)
                    except Exception:
                        pass
                    # schedule for new next
                    await _schedule_skip_sequence(chat_id, thread_id, key, next_entry)
        except asyncio.CancelledError:
            return
        except Exception:
            return
        finally:
            if key in _pending_timers:
                _pending_timers[key].pop(user_id, None)

    task = asyncio.create_task(sequence())
    _pending_timers[key][user_id] = {"task": task, "warning_sent": False}

# ---------------- Commands ----------------

@dp.message(Command("standup"))
async def cmd_standup(message: types.Message):
    """
    Add user to queue. If became first -> notify and schedule timers.
    If first and single -> immediately instruct to press /takereport.
    """
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)
    username = user.username
    first_name = user.first_name or ""

    async with _get_chat_lock(key):
        queue = await load_queue_for_key(key)
        if _find_index_by_user(queue, uid) is not None:
            await message.reply("Ты уже в очереди ✅")
            return

        entry = {
            "user_id": uid,
            "username": username,
            "first_name": first_name,
            "status": "waiting",  # waiting | in_report
            "joined_at": datetime.utcnow().isoformat(),
        }
        queue.append(entry)
        await save_queue_for_key(key, queue)

        # If became first (len==1) -> immediate instruction (per choice 1:B)
        if len(queue) == 1:
            mention = _mention_html(entry)
            text = f"{mention} Ты первый(ая). Когда зайдешь в отчет, нажми /takereport"
            try:
                await bot.send_message(chat.id, text, parse_mode=ParseMode.HTML, message_thread_id=thread_id)
            except Exception:
                await message.reply("Ты первый(ая). Когда зайдешь в отчет, нажми /takereport")
            # schedule skip timers for this first user
            await _schedule_skip_sequence(chat.id, thread_id, key, entry)
        else:
            # Inform with total count (choice D earlier)
            await message.reply(f"Добавил тебя в очередь. Сейчас в очереди {len(queue)} человек(а).")

@dp.message(Command("takereport"))
async def cmd_takereport(message: types.Message):
    """
    Can be used only by first. Marks status in_report (does not remove).
    Cancels pending timers for this user. If warning was sent, special text is sent.
    """
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    async with _get_chat_lock(key):
        queue = await load_queue_for_key(key)
        if not queue:
            await message.reply("Очередь пуста.")
            return
        idx = _find_index_by_user(queue, uid)
        if idx is None:
            await message.reply("Пока ты не в очереди.")
            return
        if idx != 0:
            await message.reply("Пока не твоя очередь 🙂 Я напишу, когда подойдет твой момент.")
            return
        if queue[0].get("status") == "in_report":
            await message.reply("Ты уже в отчете. Когда закончишь, нажми /finished")
            return

        # Cancel pending timers
        warning_was_sent = False
        if key in _pending_timers and uid in _pending_timers[key]:
            warning_was_sent = _pending_timers[key][uid].get("warning_sent", False)
        await _cancel_pending_for_user(key, uid)

        # mark in_report and keep in queue (do not remove)
        queue[0]["status"] = "in_report"
        await save_queue_for_key(key, queue)

        # reply to takereport
        await message.reply("Ты взял(а) отчет. Когда закончишь, нажми /finished")

        # if warning was sent before they pressed -> send fun text
        if warning_was_sent:
            await message.reply("Слава богу ты пришел, ахахах")

@dp.message(Command("finished"))
async def cmd_finished(message: types.Message):
    """
    Can be used only if user is first and in_report.
    Removes first, cancels timers, notifies next and schedules timers for next.
    """
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    async with _get_chat_lock(key):
        queue = await load_queue_for_key(key)
        if not queue:
            await message.reply("Очередь пустая. Чтобы встать в очередь нажми /standup")
            return
        idx = _find_index_by_user(queue, uid)
        if idx is None:
            await message.reply("Тебя нет в очереди.")
            return
        if idx != 0:
            await message.reply("Сначала дождись своей очереди 🙂")
            return
        if queue[0].get("status") != "in_report":
            await message.reply("Сначала зайди в отчет через /takereport, затем используй /finished.")
            return

        # remove first
        finished_entry = queue.pop(0)
        await save_queue_for_key(key, queue)

        # cancel any timers for finished user
        await _cancel_pending_for_user(key, uid)

        # Do NOT tag the one who finished (per spec)
        # Notify next if exists
        if queue:
            next_entry = queue[0]
            next_mention = _mention_html(next_entry)
            notify_text = f"{next_mention} Твоя очередь. Когда зайдешь в отчет, нажми /takereport"
            try:
                await bot.send_message(chat.id, notify_text, parse_mode=ParseMode.HTML, message_thread_id=thread_id)
            except Exception:
                await message.reply(notify_text, parse_mode=ParseMode.HTML)
            # schedule skip sequence for the new next
            await _schedule_skip_sequence(chat.id, thread_id, key, next_entry)
        else:
            # if queue empty -> silent
            pass

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    """
    Delete self from queue if not first. If first -> forbidden and suggest /finished.
    """
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    user = message.from_user
    uid = int(user.id)

    async with _get_chat_lock(key):
        queue = await load_queue_for_key(key)
        idx = _find_index_by_user(queue, uid)
        if idx is None:
            await message.reply("Тебя нет в очереди 😉")
            return
        if idx == 0:
            await message.reply("Ты не можешь себя удалить из очереди, так как сейчас твоя очередь. Чтобы пропустить используй /finished")
            return

        removed = queue.pop(idx)
        await save_queue_for_key(key, queue)
        await _cancel_pending_for_user(key, uid)
        await message.reply("Я устал ждать тебя и удалил из очереди. Теперь, чтобы вернуться в очередь нажми /standup")

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    """
    Show queue. Format:
    1) @ivan (в отчёте)
    2) @petr
    """
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    key = _chat_key(chat.id, thread_id)

    async with _get_chat_lock(key):
        queue = await load_queue_for_key(key)
        if not queue:
            await message.reply("Сейчас никого нет в очереди. Нажми /standup, чтобы начать.")
            return
        lines = []
        for i, e in enumerate(queue, start=1):
            disp = _entry_display(e)
            if i == 1 and e.get("status") == "in_report":
                lines.append(f"{i}) {disp} (в отчете)")
            else:
                lines.append(f"{i}) {disp}")
        await message.reply("\n".join(lines))

# ---------------- Startup / Shutdown ----------------
async def _cancel_all_pending():
    for key, per in list(_pending_timers.items()):
        for uid, info in list(per.items()):
            task = info.get("task")
            if task and not task.done():
                task.cancel()
    _pending_timers.clear()

async def main():
    await _ensure_storage_exists()
    try:
        await dp.start_polling(bot)
    finally:
        await _cancel_all_pending()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
