import os
import time
import asyncio
import re
import calendar
from datetime import datetime, timedelta, timezone, date
from typing import Dict, Tuple, List, Optional

import requests
import aiohttp
from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    BotCommand,
    Message,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.error import Conflict, TimedOut, NetworkError, BadRequest

BOT_NAME = "dektrian_online_bot"

# ========= ENV =========
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
CHAT_IDS = [c.strip() for c in _raw_chats.split(",") if c.strip()]
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# === Google Tasks (для расписания) ===
GOOGLE_TASKS_CLIENT_ID = os.getenv("GOOGLE_TASKS_CLIENT_ID", "").strip()
GOOGLE_TASKS_CLIENT_SECRET = os.getenv("GOOGLE_TASKS_CLIENT_SECRET", "").strip()
GOOGLE_TASKS_REFRESH_TOKEN = os.getenv("GOOGLE_TASKS_REFRESH_TOKEN", "").strip()
GOOGLE_TASKS_LIST_ID = os.getenv("GOOGLE_TASKS_LIST_ID", "").strip()

# === YouTube/Twitch ===
YT_API_KEY = os.getenv("YT_API_KEY", "").strip()
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "").strip()
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME", "dektrian_tv").strip()

# === Параметры вебхука ===
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "8080")))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dektrian-secret")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", f"/telegram/{BOT_NAME}")

# >>> Dektrian chat config >>>
STREAM_GROUP_TAG = "@dektrian_tv"
STREAM_CHANNEL_TAG = "@dektrian_family"
TEST_CHAT_TAG = os.getenv("TEST_CHAT_TAG", "-1001915244120").strip()
# <<< Dektrian chat config <<<

# ========= КОНФИГ В КОДЕ =========
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()
SCHEDULE_IMAGE_URL = "https://ibb.co/dsky513D"

_base = [STREAM_GROUP_TAG] + ([TEST_CHAT_TAG] if TEST_CHAT_TAG else []) + CHAT_IDS
CHAT_IDS = list(dict.fromkeys([x for x in _base if x]))

ANNOUNCE_CHAT_IDS: List[int | str] = list(dict.fromkeys(
    [STREAM_CHANNEL_TAG, STREAM_GROUP_TAG] + ([TEST_CHAT_TAG] if TEST_CHAT_TAG else [])
))
TEST_ANNOUNCE_CHAT_IDS: List[int | str] = list(dict.fromkeys(
    ([TEST_CHAT_TAG] if TEST_CHAT_TAG else [])
))
LIVE_REMINDER_CHAT_IDS: List[int | str] = list(dict.fromkeys(
    [STREAM_GROUP_TAG] + ([TEST_CHAT_TAG] if TEST_CHAT_TAG else [])
))
SCHEDULE_REMINDER_CHAT_IDS: List[int | str] = list(dict.fromkeys(
    [STREAM_GROUP_TAG] + ([TEST_CHAT_TAG] if TEST_CHAT_TAG else [])
))

DAILY_SCHEDULE_TIMES = ["12:13"]
LIVE_REMINDER_EVERY_MIN = 60
MUTE_SERVICE_MESSAGES = True
KB_LABEL = "👉  Расписание стримов и прочее  👈"
KB_LABEL_LOWER = KB_LABEL.lower()
MENU_TTL_SECONDS = 15 * 60

# ========= In-memory state =========
last_twitch_stream_id: Optional[str] = None
_tw_token: Optional[str] = None
_tw_token_expire_at: int = 0
_last_called_ts = {"tw": 0}

_user_menu_anchor: Dict[Tuple[int, int], int] = {}
_menu_timers: Dict[Tuple[int, int], asyncio.Task] = {}

_live_reminder_task: Optional[asyncio.Task] = None
_live_last_msg_by_chat: Dict[int | str, int] = {}
_last_youtube_live_id: Optional[str] = None

_posted_daily_keys: set[str] = set()

# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _ids_or_default(custom: List[int | str]) -> List[int | str]:
    return custom if custom else CHAT_IDS

# ==================== TELEGRAM UI ====================
def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(KB_LABEL)]],
                               resize_keyboard=True, is_persistent=True, one_time_keyboard=False)

# ==================== GOOGLE TASKS ====================
def _tasks_get_access_token() -> Optional[str]:
    if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
        return None
    try:
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_TASKS_CLIENT_ID,
                "client_secret": GOOGLE_TASKS_CLIENT_SECRET,
                "refresh_token": GOOGLE_TASKS_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        print(f"[TASKS] token error: {e}")
        return None

def _tasks_fetch_all() -> List[dict]:
    token = _tasks_get_access_token()
    if not token:
        return []
    items: List[dict] = []
    page_token = None
    try:
        while True:
            params = {"showCompleted": "false", "showDeleted": "false", "maxResults": "100"}
            if page_token:
                params["pageToken"] = page_token
            r = requests.get(
                f"https://tasks.googleapis.com/tasks/v1/lists/{GOOGLE_TASKS_LIST_ID}/tasks",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=20,
            )
            r.raise_for_status()
            data = r.json() or {}
            items.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        print(f"[TASKS] fetch error: {e}")
    return items

_time_re = re.compile(r"(^|\s)(\d{1,2}):(\d{2})(\b)")
_mention_re = re.compile(r"@\w+")

def _clean_title(title: str) -> str:
    if not title:
        return "Без названия"
    t = _mention_re.sub("", title)
    t = re.sub(r"\s{2,}", " ", t).strip(" —-").strip()
    return t or "Без названия"

def _extract_time_from_title(title: str) -> Tuple[Optional[str], str]:
    title = title or ""
    m = _time_re.search(title)
    if not m:
        return None, _clean_title(title)
    hhmm = f"{m.group(2)}:{m.group(3)}"
    cleaned = (title[:m.start()].strip() + " " + title[m.end():].strip()).strip()
    return hhmm, _clean_title(cleaned)

def _due_to_local_date(due_iso: str) -> Optional[date]:
    if not due_iso:
        return None
    try:
        dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
        dt_local = dt.astimezone(timezone(timedelta(hours=TZ_OFFSET_HOURS)))
        return dt_local.date()
    except Exception:
        try:
            return datetime.strptime(due_iso[:10], "%Y-%m-%d").date()
        except Exception:
            return None

def _tasks_by_date_map(tasks: List[dict]) -> Dict[date, List[dict]]:
    out: Dict[date, List[dict]] = {}
    for t in tasks:
        d = _due_to_local_date(t.get("due") or "")
        if not d:
            continue
        out.setdefault(d, []).append(t)
    return out

def _weekday_abr(d: date) -> str:
    ru_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return ru_days[d.weekday()]

def _format_today_plain(tasks: List[dict], d: date) -> str:
    header = f"📅 Стримы сегодня — {d.strftime('%d.%m.%Y')}"
    if not tasks:
        return f"{header}\n\nСегодня стримов нет."
    lines = [header, ""]
    tasks_sorted = sorted(tasks, key=lambda t: (_extract_time_from_title(t.get("title") or "")[0] or "99:99"))
    for t in tasks_sorted:
        hhmm, cleaned_title = _extract_time_from_title(t.get("title") or "")
        if hhmm:
            lines.append(f"▫️ {hhmm} — {cleaned_title}")
        else:
            lines.append(f"▫️ {cleaned_title}")
    lines.append("\nЗалетай на стримчики! 🔥")
    return "\n".join(lines)

def _format_table_for_range(tasks: List[dict], start: date, end: date, title: str) -> str:
    m = _tasks_by_date_map(tasks)
    lines = [html_escape(title), "", "<pre>", "Дата     Дн  Время  Событие", "------- ---- ------ ------------"]
    for d in _daterange_days(start, end):
        day = d.strftime("%d.%m")
        wd = _weekday_abr(d)
        day_tasks = m.get(d, [])
        if not day_tasks:
            lines.append(f"{day:8} {wd:3} {'--':5}  нет стримов")
            continue
        day_tasks_sorted = sorted(day_tasks, key=lambda t: (_extract_time_from_title(t.get('title') or "")[0] or "99:99"))
        first = True
        for t in day_tasks_sorted:
            hhmm, cleaned_title = _extract_time_from_title(t.get("title") or "")
            time_str = hhmm or "--"
            if first:
                lines.append(f"{day:8} {wd:3} {time_str:5}  {html_escape(cleaned_title)}")
                first = False
            else:
                lines.append(f"{'':8} {'':3} {time_str:5}  {html_escape(cleaned_title)}")
    lines.append("</pre>")
    return "\n".join(lines)

def _daterange_days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

# ==================== YOUTUBE ====================
def _yt_fetch_live_once() -> Optional[dict]:
    global _last_youtube_live_id
    if not (YT_API_KEY and YT_CHANNEL_ID):
        return None
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "snippet", "channelId": YT_CHANNEL_ID, "eventType": "live", "type": "video",
                    "maxResults": 1, "order": "date", "key": YT_API_KEY},
            timeout=20,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return None
        video_id = items[0]["id"]["videoId"]
        _last_youtube_live_id = video_id
        yt_title = items[0]["snippet"].get("title") or "LIVE on YouTube"
        r2 = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "snippet", "id": video_id, "key": YT_API_KEY, "maxResults": 1},
            timeout=20,
        )
        r2.raise_for_status()
        vitems = r2.json().get("items", [])
        thumb_url = None
        if vitems:
            thumbs = (vitems[0].get("snippet") or {}).get("thumbnails") or {}
            for k in ("maxres", "standard", "high", "medium", "default"):
                if k in thumbs and thumbs[k].get("url"):
                    thumb_url = thumbs[k]["url"]
                    break
        return {"id": video_id, "title": yt_title, "thumb": thumb_url}
    except Exception as e:
        print(f"[YT] error: {e}")
    return None

async def yt_fetch_live_with_retries(max_attempts: int = 3, delay_seconds: int = 10) -> Optional[dict]:
    for attempt in range(1, max_attempts + 1):
        res = await asyncio.to_thread(_yt_fetch_live_once)
        if res:
            return res
        if attempt < max_attempts:
            await asyncio.sleep(delay_seconds)
    return None

# ==================== TWITCH ====================
def _tw_fetch_token() -> Optional[str]:
    global _tw_token, _tw_token_expire_at
    now_ts = int(time.time())
    if _tw_token and now_ts < _tw_token_expire_at - 60:
        return _tw_token
    try:
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "grant_type": "client_credentials"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        _tw_token = data["access_token"]
        _tw_token_expire_at = now_ts + int(data.get("expires_in", 3600))
        return _tw_token
    except Exception as e:
        _tw_token = None
        _tw_token_expire_at = 0
    return None

def twitch_check_live() -> Optional[dict]:
    global last_twitch_stream_id
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET and TWITCH_USERNAME):
        return None
    tk = _tw_fetch_token()
    if not tk:
        return None
    def _call() -> Optional[dict]:
        r = requests.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_login": TWITCH_USERNAME},
            headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {tk}"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return None
        s = data[0]
        sid = s.get("id")
        title = s.get("title")
        if sid and sid != last_twitch_stream_id:
            return {"id": sid, "title": title}
        return None
    try:
        res = _call()
        if res:
            last_twitch_stream_id = res["id"]
        return res
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            global _tw_token, _tw_token_expire_at
            _tw_token = None
            _tw_token_expire_at = 0
            try:
                res = _call()
                if res:
                    last_twitch_stream_id = res["id"]
                return res
            except Exception:
                return None
    except Exception:
        pass
    return None

def twitch_is_live() -> bool:
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET and TWITCH_USERNAME):
        return False
    tk = _tw_fetch_token()
    if not tk:
        return False
    try:
        r = requests.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_login": TWITCH_USERNAME},
            headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {tk}"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return bool(data)
    except Exception:
        return False

# ==================== ПОСТИНГ ====================
def build_watch_kb_for_reminder() -> InlineKeyboardMarkup:
    yt_url = (f"https://www.youtube.com/watch?v={_last_youtube_live_id}"
              if _last_youtube_live_id else
              (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv"))
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("❤️ YouTube", url=yt_url),
                                  InlineKeyboardButton("💜 Twitch",  url=tw_url)]])

def build_announce_kb(youtube_video_id: Optional[str]) -> InlineKeyboardMarkup:
    yt_url = (f"https://www.youtube.com/watch?v={youtube_video_id}"
              if youtube_video_id else
              (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv"))
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ Гоу на YouTube", url=yt_url),
         InlineKeyboardButton("💜 Гоу на Twitch",  url=tw_url)],
        [InlineKeyboardButton("💸 Гоу Донатик", url="https://new.donatepay.ru/@Dektrian_TV"),
         InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")]
    ])

async def tg_broadcast_photo_first(app: Application, chat_ids: List[int | str], text: str,
                                   kb: Optional[InlineKeyboardMarkup], photo_url: str,
                                   silent: bool = False):
    for chat_id in chat_ids:
        try:
            await app.bot.send_photo(
                chat_id=chat_id,
                photo=photo_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_notification=silent,
            )
            continue
        except BadRequest:
            pass
        except Exception:
            pass
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"{photo_url}\n\n{text}",
                parse_mode="HTML",
                reply_markup=kb,
                disable_notification=silent,
                disable_web_page_preview=False,
            )
        except Exception:
            pass

async def _announce_with_sources(app: Application, title: str, yt_video: Optional[dict]):
    yt_id = yt_video["id"] if yt_video else None
    photo_url = (yt_video.get("thumb") if (yt_video and yt_video.get("thumb")) else STATIC_IMAGE_URL)
    text = (
        "🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{html_escape(title or '')}</b>\n\n"
        "#DEKTRIAN #D13 #СТРИМ"
    )
    await tg_broadcast_photo_first(app, ANNOUNCE_CHAT_IDS, text, build_announce_kb(yt_id), photo_url, silent=False)

# ЕЖЕЧАСНЫЕ НАПОМИНАНИЯ ПО ЛАЙВУ
async def _live_reminder_loop(app: Application):
    global _live_reminder_task
    try:
        while True:
            await asyncio.sleep(max(1, LIVE_REMINDER_EVERY_MIN * 60))
            is_live = await asyncio.to_thread(twitch_is_live)
            if not is_live:
                break
            for chat_id, mid in list(_live_last_msg_by_chat.items()):
                try:
                    await app.bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception:
                    pass
            kb = build_watch_kb_for_reminder()
            for chat_id in LIVE_REMINDER_CHAT_IDS:
                try:
                    msg = await app.bot.send_message(
                        chat_id=chat_id,
                        text="Мы всё ещё на стриме, врывайся! 😏",
                        reply_markup=kb,
                        disable_notification=False,
                    )
                    _live_last_msg_by_chat[chat_id] = msg.message_id
                except Exception:
                    pass
    finally:
        _live_reminder_task = None
        _live_last_msg_by_chat.clear()

def _start_live_reminders_if_needed(app: Application):
    global _live_reminder_task
    if _live_reminder_task and not _live_reminder_task.done():
        return
    _live_reminder_task = asyncio.create_task(_live_reminder_loop(app))

# ДНЕВНЫЕ НАПОМИНАНИЯ РАСПИСАНИЯ
async def _daily_schedule_loop(app: Application):
    while True:
        try:
            now = now_local()
            hhmm = now.strftime("%H:%M")
            if hhmm in DAILY_SCHEDULE_TIMES:
                key = now.strftime("%Y-%m-%d ") + hhmm
                if key not in _posted_daily_keys:
                    _posted_daily_keys.add(key)
                    await _post_today_schedule_if_any(app)
            await asyncio.sleep(30)
        except Exception:
            await asyncio.sleep(5)

async def _post_today_schedule_if_any(app: Application):
    tasks = await asyncio.to_thread(_tasks_fetch_all)
    today = now_local().date()
    todays = [t for t in tasks if _due_to_local_date(t.get("due") or "") == today]
    if not todays:
        return
    text = _format_today_plain(todays, today)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")]])
    await tg_broadcast_photo_first(app, SCHEDULE_REMINDER_CHAT_IDS, text, kb, SCHEDULE_IMAGE_URL, silent=False)

# ==================== ЯДРО: «будильник» ====================
async def minute_loop(app: Application):
    while True:
        try:
            if _sec_since(_last_called_ts["tw"]) >= 60:
                tw = await asyncio.to_thread(twitch_check_live)
                if tw:
                    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
                    title = tw.get("title") or (yt_live.get("title") if yt_live else "Стрим")
                    await _announce_with_sources(app, title, yt_live)
                    _start_live_reminders_if_needed(app)
                _last_called_ts["tw"] = int(time.time())
        except Exception:
            pass
        await asyncio.sleep(5)

async def self_ping():
    if not PUBLIC_URL:
        return
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{PUBLIC_URL}/_wake", timeout=10) as resp:
                    _ = await resp.text()
        except Exception:
            pass
        await asyncio.sleep(600)

# ==================== ИНЛАЙН-МЕНЮ ====================
def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="menu|today"),
         InlineKeyboardButton("📅 Неделя",  callback_data="menu|week")],
        [InlineKeyboardButton("📅 Месяц",   callback_data="menu|month"),
         InlineKeyboardButton("📳 Соцсети",    callback_data="menu|socials")],
        [InlineKeyboardButton("Бронь стрима", callback_data="br|main"),
         InlineKeyboardButton("💎 Купить юси",   url="https://t.me/EtherealVisionShop")],
        [InlineKeyboardButton("🚸 Вступить в клан 🚸", url="https://t.me/D13_join_bot")],
    ])

def _socials_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("YouTube", url="https://www.youtube.com/@Dektrian_TV"),
         InlineKeyboardButton("Twitch",  url=f"https://www.twitch.tv/{TWITCH_USERNAME}")],
        [InlineKeyboardButton("Группа Telegram", url="https://t.me/dektrian_tv"),
         InlineKeyboardButton("Канал Telegram",  url="https://t.me/dektrian_family")],
        [InlineKeyboardButton("TikTok", url="https://www.tiktok.com/@dektrian_tv")],
        [InlineKeyboardButton("← Назад", callback_data="menu|main")],
    ]
    return InlineKeyboardMarkup(rows)

BRONE_TERMS = (
    "<b>Условия бронирования:</b>\n"
    "   \n"
    "• Призовые кастомки — <b>бесплатно</b> при условиях:\n"
    "  — от 3 кастомных игр;\n"
    "  — приз <b>480 UC</b> за каждую карту;\n"
    "  — свободный вход.\n"
    "• Турниры / лиги / праки — от <b>250₽ / 125₴</b> за 1 катку (по договорённости).\n"
    "• TDM-турниры — от <b>100₽ / 50₴</b> за катку.\n"
    "• ВОВ-турниры — Цена договорная.\n"
    "   \n"
    "<b>Подробнее тут: https://t.me/dektrian_tv/151271</b>\n"
)

def _brone_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Условия брони", callback_data="br|terms"),
         InlineKeyboardButton("Сделать бронь", url="https://t.me/DektrianTV")],
        [InlineKeyboardButton("← Назад", callback_data="menu|main")]
    ])

def _month_weeks(year: int, month: int) -> List[Tuple[date, date]]:
    last_day = calendar.monthrange(year, month)[1]
    weeks = []
    d = date(year, month, 1)
    while d.month == month:
        start = d
        end = min(date(year, month, last_day), start + timedelta(days=6))
        weeks.append((start, end))
        d = end + timedelta(days=1)
    return weeks

def _month_title(year: int, month: int, idx: int, total: int) -> str:
    ru_months = ["","Январь","Февраль","Март","Апрель","Май","Июнь","Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    return f"📆 {ru_months[month]} {year} — Неделя {idx+1}/{total}"

def _month_kb(ym: str, idx: int, total: int) -> InlineKeyboardMarkup:
    prev_idx = (idx - 1) % total
    next_idx = (idx + 1) % total
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️", callback_data=f"m|{ym}|{prev_idx}"),
         InlineKeyboardButton(f"Неделя {idx+1}/{total}", callback_data=f"m|{ym}|{idx}"),
         InlineKeyboardButton("▶️", callback_data=f"m|{ym}|{next_idx}")],
        [InlineKeyboardButton("← Меню", callback_data="menu|main")]
    ])

# ==================== TTL МЕНЮ ====================
def _cancel_menu_timer(chat_id: int, message_id: int):
    key = (chat_id, message_id)
    task = _menu_timers.pop(key, None)
    if task and not task.done():
        task.cancel()

def _find_anchor_key_by_message(chat_id: int, message_id: int) -> Optional[Tuple[int, int]]:
    for (c, u), mid in list(_user_menu_anchor.items()):
        if c == chat_id and mid == message_id:
            return (c, u)
    return None

async def _menu_ttl_worker(chat_id: int, message_id: int):
    try:
        await asyncio.sleep(MENU_TTL_SECONDS)
        try:
            await app_global.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        ak = _find_anchor_key_by_message(chat_id, message_id)
        if ak:
            _user_menu_anchor.pop(ak, None)
    finally:
        _menu_timers.pop((chat_id, message_id), None)

def _arm_menu_ttl(chat_id: int, message_id: int):
    _cancel_menu_timer(chat_id, message_id)
    task = asyncio.create_task(_menu_ttl_worker(chat_id, message_id))
    _menu_timers[(chat_id, message_id)] = task

def _extend_menu_ttl(chat_id: int, message_id: int):
    _arm_menu_ttl(chat_id, message_id)

# ==================== РЕНДЕРЫ ТЕКСТОВ РАСПИСАНИЯ ====================
async def _ensure_tasks_env(update: Optional[Update]) -> bool:
    if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❗ Не настроен доступ к Google Tasks. Нужны GOOGLE_TASKS_CLIENT_ID / SECRET / REFRESH_TOKEN / LIST_ID в ENV.",
                reply_markup=main_reply_kb(),
                disable_notification=MUTE_SERVICE_MESSAGES,
            )
        return False
    return True

async def _render_today_text() -> str:
    tasks = await asyncio.to_thread(_tasks_fetch_all)
    d = now_local().date()
    todays = [t for t in tasks if _due_to_local_date(t.get("due") or "") == d]
    return _format_today_plain(todays, d)

async def _render_week_text() -> str:
    tasks = await asyncio.to_thread(_tasks_fetch_all)
    start = now_local().date()
    end = start + timedelta(days=6)
    return _format_table_for_range(tasks, start, end, f"🗓 Неделя — {start.strftime('%d.%m')}–{end.strftime('%d.%m')}")

async def _render_month_text(idx: int | None = None) -> Tuple[str, InlineKeyboardMarkup]:
    tasks = await asyncio.to_thread(_tasks_fetch_all)
    today = now_local().date()
    year, month = today.year, today.month
    weeks = _month_weeks(year, month)
    i = idx if idx is not None else 0
    i = max(0, min(i, len(weeks) - 1))
    start, end = weeks[i]
    text = _format_table_for_range(tasks, start, end, _month_title(year, month, i, len(weeks)))
    kb = _month_kb(f"{year:04d}-{month:02d}", i, len(weeks))
    return text, kb

# ==================== ПОКАЗ МЕНЮ ====================
async def _show_main_menu_for_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    anchor_key = (chat_id, user_id)

    try:
        if update.effective_message:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.effective_message.message_id)
    except Exception:
        pass

    old_msg_id = _user_menu_anchor.get(anchor_key)
    if old_msg_id:
        _cancel_menu_timer(chat_id, old_msg_id)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
        except Exception:
            pass
        _user_menu_anchor.pop(anchor_key, None)

    try:
        msg = await context.bot.send_message(chat_id=chat_id, text="Меню бота:",
                                             reply_markup=_main_menu_kb(),
                                             disable_notification=MUTE_SERVICE_MESSAGES)
        _user_menu_anchor[anchor_key] = msg.message_id
        _arm_menu_ttl(chat_id, msg.message_id)
    except Exception:
        pass

# ==================== КОМАНДЫ ====================
async def cmd_test1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    yt_id = yt_live["id"] if yt_live else None
    photo_url = (yt_live.get("thumb") if yt_live and yt_live.get("thumb") else STATIC_IMAGE_URL)
    text = f"🔴 <b>{html_escape(title)}</b>\n\n#TEST #D13"
    await tg_broadcast_photo_first(context.application, TEST_ANNOUNCE_CHAT_IDS, text, build_announce_kb(yt_id), photo_url, silent=False)
    if update.effective_message:
        await update.effective_message.reply_text("✅ Тест: отправил анонс в тест-группу.", disable_notification=MUTE_SERVICE_MESSAGES)

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    text = await _render_today_text()
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML", disable_notification=MUTE_SERVICE_MESSAGES)

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    text = await _render_week_text()
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML", disable_notification=MUTE_SERVICE_MESSAGES)

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    text, kb = await _render_month_text(0)
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_notification=MUTE_SERVICE_MESSAGES)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_main_menu_for_user(update, context)

# ==================== РОУТИНГ ====================
async def on_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_message.text:
        return
    text = update.effective_message.text.strip().lower()
    if text == KB_LABEL_LOWER:
        await _show_main_menu_for_user(update, context)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    chat_id = q.message.chat.id
    msg_id = q.message.message_id
    _extend_menu_ttl(chat_id, msg_id)

    if data == "menu|main":
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                text="Меню бота:", reply_markup=_main_menu_kb())
        except Exception:
            pass
        return

    if data == "menu|today":
        if not await _ensure_tasks_env(None):
            return
        text = await _render_today_text()
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                text=text, parse_mode="HTML",
                                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Меню", callback_data="menu|main")]]))
        except Exception:
            pass
        return

    if data == "menu|week":
        if not await _ensure_tasks_env(None):
            return
        text = await _render_week_text()
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                text=text, parse_mode="HTML",
                                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Меню", callback_data="menu|main")]]))
        except Exception:
            pass
        return

    if data == "menu|month":
        if not await _ensure_tasks_env(None):
            return
        text, kb = await _render_month_text(0)
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                text=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return

    if data.startswith("m|"): 
        try:
            _, ym, idx_str = data.split("|")
            year, month = map(int, ym.split("-"))
            idx = int(idx_str)
        except Exception:
            return
        tasks = await asyncio.to_thread(_tasks_fetch_all)
        weeks = _month_weeks(year, month)
        if not weeks:
            return
        idx = max(0, min(idx, len(weeks)-1))
        start, end = weeks[idx]
        text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
        kb = _month_kb(ym, idx, len(weeks))
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                text=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return

    if data == "menu|socials":
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                text="Соцсети стримера:", reply_markup=_socials_kb())
        except Exception:
            pass
        return

    if data == "br|main":
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                text="Бронь стрима:", reply_markup=_brone_kb())
        except Exception:
            pass
        return

    if data == "br|terms":
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                text=BRONE_TERMS, parse_mode="HTML", reply_markup=_brone_kb())
        except Exception:
            pass
        return

# ==================== ERROR-HANDLER ====================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        print("[HOOK] Conflict (setWebhook race?).")
        return
    if isinstance(err, (TimedOut, NetworkError)):
        print(f"[HOOK] transient error: {err}")
        return
    print(f"[HOOK] unhandled error: {err}")

# ==================== STARTUP ====================
app_global: Application

async def _on_start(app: Application):
    global app_global
    app_global = app

    await app.bot.set_my_commands([
        BotCommand("today", "📅 Стримы сегодня"),
        BotCommand("week",  "🗓 Стримы на неделю"),
        BotCommand("month", "📆 Стримы за месяц"),
        BotCommand("menu",  "Открыть меню"),
    ])

    for chat_id in _ids_or_default([]):
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text="Клавиатура активна. Нажми «Расписание стримов и прочее» ⤵️",
                reply_markup=main_reply_kb(),
                disable_notification=MUTE_SERVICE_MESSAGES,
            )
        except Exception:
            pass

    asyncio.create_task(minute_loop(app))
    asyncio.create_task(self_ping())
    asyncio.create_task(_daily_schedule_loop(app))
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")

# ==================== APP MAIN ====================
async def start_bot():
    if not TG_TOKEN or not CHAT_IDS:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS in Environment")
    if not PUBLIC_URL:
        raise SystemExit("Set PUBLIC_URL (https://<your-host>) for webhook")

    application = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(_on_start)
        .build()
    )

    application.add_handler(CommandHandler("test1", cmd_test1))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week",  cmd_week))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("menu",  cmd_menu))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_buttons))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_error_handler(on_error)

    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    
    # 1. Настройка Webhook для Telegram
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )
    
    # 2. Инициализация и запуск контекста Telegram бота
    await application.initialize()
    await application.start()

    # 3. Настройка кастомного веб-сервера aiohttp для Webhook и Render Health Check
    web_app = web.Application()

    async def health_check(request):
        return web.Response(text="OK - Dektrian Bot is alive", status=200)

    # Вебхук для Телеграма
    web_app.router.add_post(WEBHOOK_PATH, application.create_webhook_handler())
    # Маршруты для Render (Health Check и пинг)
    web_app.router.add_get("/", health_check)
    web_app.router.add_get("/_wake", health_check)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"[WEBHOOK] listen 0.0.0.0:{PORT}  path={WEBHOOK_PATH}  url={webhook_url}")

    # Блокировка, чтобы сервер не закрывался
    await asyncio.Event().wait()

def main():
    asyncio.run(start_bot())

if __name__ == "__main__":
    main()
