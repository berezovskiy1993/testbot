# bot.py
import os
import time
import asyncio
import re
import calendar
import textwrap
from datetime import datetime, timedelta, timezone, date

import requests
import aiohttp  # self-ping
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    BotCommand,
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

# Общий список чатов (по умолчанию берём из переменной окружения)
_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
CHAT_IDS = [c.strip() for c in _raw_chats.split(",") if c.strip()]

# Раздельные списки (можете редактировать прямо здесь)
# Если оставить пустыми, код будет использовать CHAT_IDS
STREAM_POST_CHAT_IDS = [c.strip() for c in (os.getenv("STREAM_POST_CHAT_IDS") or "").split(",") if c.strip()]  # анонсы/ежечасные
SCHEDULE_POST_CHAT_IDS = [c.strip() for c in (os.getenv("SCHEDULE_POST_CHAT_IDS") or "").split(",") if c.strip()]  # дневные напоминалки

# Киев: летом UTC+3, зимой UTC+2 — вручную
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# === Google Tasks (для команд и напоминалок) ===
GOOGLE_TASKS_CLIENT_ID = os.getenv("GOOGLE_TASKS_CLIENT_ID", "").strip()
GOOGLE_TASKS_CLIENT_SECRET = os.getenv("GOOGLE_TASKS_CLIENT_SECRET", "").strip()
GOOGLE_TASKS_REFRESH_TOKEN = os.getenv("GOOGLE_TASKS_REFRESH_TOKEN", "").strip()
GOOGLE_TASKS_LIST_ID = os.getenv("GOOGLE_TASKS_LIST_ID", "").strip()

# YouTube (используются по триггеру старта на Twitch)
YT_API_KEY = os.getenv("YT_API_KEY", "").strip()
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "").strip()

# Twitch
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME", "dektrian_tv").strip()

# Картинки
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()  # для анонса стрима, если нет превью YouTube
SCHEDULE_REMINDER_IMAGE_URL = os.getenv("SCHEDULE_REMINDER_IMAGE_URL", "https://ibb.co/LXSMV1FQ").strip()  # для дневных напоминаний

# Соцсети (дефолты можно править тут)
SOC_YT = os.getenv("SOCIAL_YOUTUBE", "https://www.youtube.com/@Dektrian_TV").strip()
SOC_TWITCH = os.getenv("SOCIAL_TWITCH", "https://www.twitch.tv/dektrian_tv").strip()
SOC_TG_GROUP = os.getenv("SOCIAL_TG_GROUP", "https://t.me/dektrian_tv").strip()
SOC_TG_CHANNEL = os.getenv("SOCIAL_TG_CHANNEL", "https://t.me/dektrian_family").strip()
SOC_TIKTOK = os.getenv("SOCIAL_TIKTOK", "https://www.tiktok.com/@dektrian_tv").strip()

# Параметры вебхука
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "8080")))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dektrian-secret")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", f"/telegram/{BOT_NAME}")

# ========= In-memory state =========
last_twitch_stream_id: str | None = None
_tw_token: str | None = None
_tw_token_expire_at: int = 0  # unix ts
_last_called_ts = {"tw": 0}

# «Личные якоря» для меню: (chat_id, user_id) -> message_id
USER_MENU_ANCHORS: dict[tuple[int, int], int] = {}

# Ежечасные напоминания о живом стриме
HOURLY = {
    "active": False,
    "interval_minutes": int(os.getenv("HOURLY_INTERVAL_MIN", "60")),  # можно менять
    "last_sent_ts": 0,
    "last_msg_ids": {},  # chat_id -> message_id (чтобы удалять старое напоминание)
    "yt_video_id": None,
}
# Время дневных напоминалок (локальное)
DAILY_REMINDER_TIMES = [t.strip() for t in (os.getenv("DAILY_REMINDER_TIMES") or "10:00,14:00").split(",") if t.strip()]
_daily_fired_cache: set[str] = set()  # например "2025-09-07 10:00"

# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _effective_stream_chats() -> list[str]:
    return STREAM_POST_CHAT_IDS or CHAT_IDS

def _effective_schedule_chats() -> list[str]:
    return SCHEDULE_POST_CHAT_IDS or CHAT_IDS

# ==================== TELEGRAM UI ====================
REPLY_BUTTON_LABEL = "📋 Расписание стримов и прочее"

def main_reply_kb() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(REPLY_BUTTON_LABEL)]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, one_time_keyboard=False)

def _main_menu_kb() -> InlineKeyboardMarkup:
    cal = "📅"
    rows = [
        [InlineKeyboardButton(f"{cal} Сегодня", callback_data="nav|today"),
         InlineKeyboardButton(f"{cal} Неделя", callback_data="nav|week")],
        [InlineKeyboardButton(f"{cal} Месяц", callback_data="nav|month")],
        [InlineKeyboardButton("Бронь стрима ➚", callback_data="menu|book") , InlineKeyboardButton("Купить юси ➚", url="https://t.me/uc_pubg_bounty")],
        [InlineKeyboardButton("Вступить в клан ➚", url="https://t.me/D13_join_bot")],
        [InlineKeyboardButton("Соцсети стримера", callback_data="menu|socials")],
    ]
    return InlineKeyboardMarkup(rows)

def _socials_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("YouTube", url=SOC_YT), InlineKeyboardButton("Twitch", url=SOC_TWITCH)],
        [InlineKeyboardButton("Группа Telegram", url=SOC_TG_GROUP), InlineKeyboardButton("Канал Telegram", url=SOC_TG_CHANNEL)],
        [InlineKeyboardButton("TikTok", url=SOC_TIKTOK)],
        [InlineKeyboardButton("← Назад", callback_data="menu|main")],
    ]
    return InlineKeyboardMarkup(rows)

def _book_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Условия брони", callback_data="book|rules")],
        [InlineKeyboardButton("Сделать бронь ➚", url="https://t.me/DektrianTV")],
        [InlineKeyboardButton("← Назад", callback_data="menu|main")],
    ]
    return InlineKeyboardMarkup(rows)

BOOK_RULES_TEXT = (
    "📌 <b>Условия бронирования</b>\n"
    "• <b>Призовые кастомки</b> — бесплатно при условии от 3 игр.\n"
    "  Приз: <b>480 UC</b> на каждую карту, свободный вход.\n"
    "• <b>Турниры/лиги/праки</b> — от <b>250₽ / 125₴</b> за карту (по договорённости).\n"
    "• <b>ТДМ-турниры</b> — от <b>100₽ / 50₴</b> за карту.\n"
    "Для брони: нажмите «Сделать бронь» и напишите мне в ЛС."
)

# ==================== GOOGLE TASKS (helpers) ====================
def _tasks_get_access_token() -> str | None:
    if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
        print("[TASKS] Missing env: CLIENT_ID/SECRET/REFRESH_TOKEN/LIST_ID")
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

def _tasks_fetch_all() -> list[dict]:
    token = _tasks_get_access_token()
    if not token:
        return []
    items: list[dict] = []
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

def _extract_time_from_title(title: str) -> tuple[str | None, str]:
    title = title or ""
    m = _time_re.search(title)
    if not m:
        return None, _clean_title(title)
    hhmm = f"{m.group(2)}:{m.group(3)}"
    cleaned = (title[:m.start()].strip() + " " + title[m.end():].strip()).strip()
    return hhmm, _clean_title(cleaned)

def _due_to_local_date(due_iso: str) -> date | None:
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

def _tasks_by_date_map(tasks: list[dict]) -> dict[date, list[dict]]:
    out: dict[date, list[dict]] = {}
    for t in tasks:
        d = _due_to_local_date(t.get("due") or "")
        if not d:
            continue
        out.setdefault(d, []).append(t)
    return out

# ======= форматирование таблиц (моно) с ручной обёрткой и выравниванием =======
# ширины колонок: "Дата"(dd.mm)=8, "Дн"=3, "Время"(hh:mm или --)=5, пробелы=2
COL_DATE = 8
COL_WD = 3
COL_TIME = 5
COL_GAP = 2
EVENT_COL_WIDTH = 48  # ширина колонки «Событие» для ручной обёртки (подбирается)

def _weekday_abr(d: date) -> str:
    return d.strftime("%a")  # ENG аббревиатуры

def _wrap_event_lines(title: str) -> list[str]:
    # переносим вручную внутри EVENT_COL_WIDTH
    wrapped = textwrap.wrap(title, width=EVENT_COL_WIDTH, break_long_words=False, break_on_hyphens=False)
    return wrapped if wrapped else [""]

def _format_table_for_range(tasks: list[dict], start: date, end: date, title: str) -> str:
    m = _tasks_by_date_map(tasks)

    lines = []
    lines.append(html_escape(title))
    lines.append("<pre>")
    lines.append("Дата     Дн  Время  Событие")
    lines.append("------- ---- ------ " + "-" * EVENT_COL_WIDTH)

    for day in (start + timedelta(n) for n in range((end - start).days + 1)):
        wd = _weekday_abr(day)
        day_tasks = sorted(m.get(day, []),
                           key=lambda t: (_extract_time_from_title(t.get('title') or "")[0] or "99:99"))
        if not day_tasks:
            prefix = f"{day.strftime('%d.%m'):8} {wd:3} {'--':5}  "
            lines.append(prefix + "нет стримов")
            continue

        first = True
        for t in day_tasks:
            hhmm, cleaned_title = _extract_time_from_title(t.get("title") or "")
            time_str = hhmm or "--"
            title_lines = _wrap_event_lines(html_escape(cleaned_title))
            if first:
                prefix = f"{day.strftime('%d.%m'):8} {wd:3} {time_str:5}  "
                lines.append(prefix + title_lines[0])
                indent = " " * (len(prefix))
                for extra in title_lines[1:]:
                    lines.append(indent + extra)
                first = False
            else:
                prefix = f"{'':8} {'':3} {time_str:5}  "
                lines.append(prefix + title_lines[0])
                indent = " " * (len(prefix))
                for extra in title_lines[1:]:
                    lines.append(indent + extra)

    lines.append("</pre>")
    return "\n".join(lines)

# ==================== YOUTUBE ====================
def _yt_fetch_live_once() -> dict | None:
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
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", "?")
        try:
            body = e.response.json()
        except Exception:
            body = getattr(e.response, "text", "")
        print(f"[YT] HTTP {code}: {body}")
    except Exception as e:
        print(f"[YT] error: {e}")
    return None

async def yt_fetch_live_with_retries(max_attempts: int = 3, delay_seconds: int = 10) -> dict | None:
    for attempt in range(1, max_attempts + 1):
        res = _yt_fetch_live_once()
        if res:
            return res
        if attempt < max_attempts:
            await asyncio.sleep(delay_seconds)
    return None

# ==================== TWITCH ====================
def _tw_fetch_token() -> str | None:
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
    except requests.HTTPError as e:
        print(f"[TW] token HTTP {getattr(e.response, 'status_code', '?')}: {getattr(e.response, 'text', '')}")
        _tw_token = None
        _tw_token_expire_at = 0
    except Exception as e:
        print(f"[TW] token error: {e}")
        _tw_token = None
        _tw_token_expire_at = 0
    return None

def twitch_check_live_new_stream() -> dict | None:
    """Возвращает {'id': stream_id, 'title': title} если появился НОВЫЙ эфир (по сравнению с last_twitch_stream_id)."""
    global last_twitch_stream_id
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET and TWITCH_USERNAME):
        return None
    tk = _tw_fetch_token()
    if not tk:
        return None

    def _call() -> dict | None:
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
            tk2 = _tw_fetch_token()
            if tk2:
                try:
                    res = _call()
                    if res:
                        last_twitch_stream_id = res["id"]
                    return res
                except Exception as e2:
                    print(f"[TW] retry failed: {e2}")
                    return None
        print(f"[TW] streams HTTP {getattr(e.response, 'status_code', '?')}: {getattr(e.response, 'text', '')}")
    except Exception as e:
        print(f"[TW] error: {e}")
    return None

def twitch_is_live_now() -> bool:
    """Просто проверка, сейчас ли онлайн Twitch."""
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
        return bool(r.json().get("data", []))
    except Exception as e:
        print(f"[TW] live-check error: {e}")
        return False

# ==================== Постинг ====================
async def tg_broadcast_photo_first(app: Application, text: str, kb: InlineKeyboardMarkup | None, photo_url: str, chat_ids: list[str], silent: bool):
    """Сначала фото (если не получится — текст с превью)."""
    for chat_id in chat_ids:
        # 1) Фото
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
        except BadRequest as e:
            print(f"[TG] photo send failed for {chat_id}: {e}. Fallback.")
        except Exception as e:
            print(f"[TG] photo send error to {chat_id}: {e}. Fallback.")

        # 2) Фолбэк текст
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"{photo_url}\n\n{text}",
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=False,
                disable_notification=silent,
            )
        except Exception as e:
            print(f"[TG] message send error to {chat_id}: {e}")

async def tg_broadcast_text(app: Application, text: str, kb: InlineKeyboardMarkup | None, chat_ids: list[str], silent: bool):
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_notification=silent,
            )
        except Exception as e:
            print(f"[TG] send error to {chat_id}: {e}")

# ==================== Анонс + ежечасные ====================
async def _announce_with_sources(app: Application, title: str, yt_video: dict | None):
    yt_id = yt_video["id"] if yt_video else None
    photo_url = (yt_video.get("thumb") if (yt_video and yt_video.get("thumb")) else STATIC_IMAGE_URL)
    text = (
        "🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{html_escape(title or '')}</b>\n\n"
        "#DEKTRIAN #D13 #ОНЛАЙН"
    )
    await tg_broadcast_photo_first(
        app,
        text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("❤️ Гоу на YouTube", url=(f"https://www.youtube.com/watch?v={yt_id}" if yt_id else SOC_YT)),
             InlineKeyboardButton("💜 Гоу на Twitch",  url=SOC_TWITCH)]
        ]),
        photo_url,
        _effective_stream_chats(),
        silent=False,  # анонс со звуком
    )
    # стартуем ежечасные
    await _start_hourly_reminders(app, yt_id)

async def _start_hourly_reminders(app: Application, yt_id: str | None):
    if HOURLY["active"]:
        HOURLY["yt_video_id"] = yt_id or HOURLY.get("yt_video_id")
        return
    HOURLY["active"] = True
    HOURLY["last_sent_ts"] = 0
    HOURLY["last_msg_ids"] = {}
    HOURLY["yt_video_id"] = yt_id
    asyncio.create_task(_hourly_loop(app))

async def _hourly_loop(app: Application):
    print("[HOURLY] started")
    try:
        while HOURLY["active"]:
            # Если оффлайн — выходим и чистим состояние
            if not twitch_is_live_now():
                print("[HOURLY] twitch offline -> stop")
                HOURLY["active"] = False
                HOURLY["last_msg_ids"].clear()
                break

            now_ts = int(time.time())
            interval = int(HOURLY["interval_minutes"]) * 60
            if now_ts - HOURLY["last_sent_ts"] >= interval:
                # Удалим прошлые напоминания
                for chat_id, msg_id in list(HOURLY["last_msg_ids"].items()):
                    try:
                        await app.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                    except Exception:
                        pass
                # Отправим новые
                yt_id = HOURLY.get("yt_video_id")
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("YouTube", url=(f"https://www.youtube.com/watch?v={yt_id}" if yt_id else SOC_YT)),
                    InlineKeyboardButton("Twitch", url=SOC_TWITCH),
                ]])
                text = "⚡ Мы всё ещё на стриме, врывайся! 😏"
                for chat_id in _effective_stream_chats():
                    try:
                        m = await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, disable_notification=False)
                        HOURLY["last_msg_ids"][chat_id] = m.message_id
                    except Exception as e:
                        print(f"[HOURLY] send error to {chat_id}: {e}")
                HOURLY["last_sent_ts"] = now_ts
            await asyncio.sleep(10)
    except Exception as e:
        print(f"[HOURLY] loop error: {e}")

# ==================== Периодические фоновые задачи ====================
async def minute_loop(app: Application):
    print(f"[WAKE] minute loop started at {now_local().isoformat()}")
    while True:
        try:
            if _sec_since(_last_called_ts["tw"]) >= 60:
                tw = twitch_check_live_new_stream()
                if tw:
                    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
                    title = tw.get("title") or (yt_live.get("title") if yt_live else "Стрим")
                    await _announce_with_sources(app, title, yt_live)
                _last_called_ts["tw"] = int(time.time())
        except Exception as e:
            print(f"[WAKE] loop error: {e}")
        await asyncio.sleep(5)

async def daily_reminders_loop(app: Application):
    print("[DAILY] reminders loop started")
    while True:
        try:
            lt = now_local()
            hm = lt.strftime("%H:%M")
            key = f"{lt.date()} {hm}"
            if hm in DAILY_REMINDER_TIMES and key not in _daily_fired_cache:
                # соберём на сегодня
                tasks = _tasks_fetch_all()
                today = lt.date()
                today_tasks = []
                for t in tasks:
                    d = _due_to_local_date(t.get("due") or "")
                    if d == today:
                        today_tasks.append(t)
                if today_tasks:
                    # Текст обычный (не моно), только кнопка «Вступить в клан»
                    # Список пунктами
                    lines = ["<b>Стримы сегодня:</b>", ""]
                    for t in sorted(today_tasks, key=lambda x: (_extract_time_from_title(x.get("title") or "")[0] or "99:99")):
                        hhmm, title = _extract_time_from_title(t.get("title") or "")
                        if hhmm:
                            lines.append(f"• {hhmm} — {html_escape(title)}")
                        else:
                            lines.append(f"• {html_escape(title)}")
                    lines.append("")
                    lines.append("Залетай на стримчики! 🔥")
                    text = "\n".join(lines)
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")]])
                    await tg_broadcast_photo_first(
                        app,
                        text,
                        kb,
                        SCHEDULE_REMINDER_IMAGE_URL,
                        _effective_schedule_chats(),
                        silent=False,  # дневные напоминалки со звуком
                    )
                _daily_fired_cache.add(key)
        except Exception as e:
            print(f"[DAILY] loop error: {e}")
        await asyncio.sleep(20)

async def self_ping():
    if not PUBLIC_URL:
        print("[SELF-PING] skipped: PUBLIC_URL is empty")
        return
    print(f"[SELF-PING] started; target={PUBLIC_URL}/_wake")
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{PUBLIC_URL}/_wake", timeout=10) as resp:
                    _ = await resp.text()
        except Exception as e:
            print(f"[SELF-PING] error: {e}")
        await asyncio.sleep(600)

# ==================== HELPERS: личные якоря меню ====================
async def _ensure_user_anchor(update: Update, context: ContextTypes.DEFAULT_TYPE, screen: str = "menu"):
    """Создаёт или возвращает личное сообщение-меню пользователя и показывает нужный экран."""
    if not update.effective_user or not update.effective_chat:
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    key = (chat_id, user_id)

    # создаём если нет
    msg_id = USER_MENU_ANCHORS.get(key)
    if not msg_id:
        try:
            m = await context.bot.send_message(
                chat_id=chat_id,
                text="Меню бота:",
                reply_markup=_main_menu_kb(),
                disable_notification=True,
            )
            USER_MENU_ANCHORS[key] = m.message_id
            msg_id = m.message_id
        except Exception as e:
            print(f"[MENU] cannot send anchor: {e}")
            return

    # показать нужный экран
    await _render_screen(context, chat_id, msg_id, screen)

async def _render_screen(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, screen: str):
    """Редактирует личное меню на конкретный экран."""
    try:
        if screen == "menu":
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="Меню бота:", reply_markup=_main_menu_kb())
            return

        if screen == "socials":
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="Соцсети стримера:", reply_markup=_socials_kb())
            return

        if screen == "book":
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="Бронь стрима:", reply_markup=_book_menu_kb())
            return

        if screen == "book_rules":
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=BOOK_RULES_TEXT, parse_mode="HTML", reply_markup=_book_menu_kb())
            return

        # Экраны расписания
        tasks = _tasks_fetch_all()
        if screen == "today":
            d = now_local().date()
            text = _format_table_for_range(tasks, d, d, f"📅 Сегодня — {d.strftime('%d.%m.%Y')}")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML", reply_markup=_main_menu_kb())
            return

        if screen == "week":
            start = now_local().date()
            end = start + timedelta(days=6)
            text = _format_table_for_range(tasks, start, end, f"📅 Неделя — {start.strftime('%d.%m')}–{end.strftime('%d.%m')}")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML", reply_markup=_main_menu_kb())
            return

        if screen.startswith("month"):
            # month[:this] или month|YYYY-MM|idx
            if screen == "month":
                today = now_local().date()
                ym = f"{today.year:04d}-{today.month:02d}"
                idx = 0
            else:
                # month|YYYY-MM|idx
                _, ym, idx_str = screen.split("|")
                idx = int(idx_str)

            year, month = map(int, ym.split("-"))
            weeks = _month_weeks(year, month)
            idx = max(0, min(idx, len(weeks) - 1))
            start, end = weeks[idx]
            text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML", reply_markup=_month_kb(ym, idx, len(weeks)))
            return

        # fallback
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="Меню бота:", reply_markup=_main_menu_kb())
    except BadRequest as e:
        # message is not modified — игнорируем
        if "message is not modified" not in str(e).lower():
            print(f"[MENU] edit error: {e}")

def _month_weeks(year: int, month: int) -> list[tuple[date, date]]:
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
    ru_months = ["", "Январь","Февраль","Март","Апрель","Май","Июнь","Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    return f"📆 {ru_months[month]} {year} — Неделя {idx+1}/{total}"

def _month_kb(ym: str, idx: int, total: int) -> InlineKeyboardMarkup:
    prev_idx = (idx - 1) % total
    next_idx = (idx + 1) % total
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️", callback_data=f"nav|month|{ym}|{prev_idx}"),
         InlineKeyboardButton(f"Неделя {idx+1}/{total}", callback_data=f"noop"),
         InlineKeyboardButton("▶️", callback_data=f"nav|month|{ym}|{next_idx}")],
        [InlineKeyboardButton("← В меню", callback_data="nav|menu")]
    ])

# ==================== КОМАНДЫ ====================
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # вызов из /menu или reply-клавиатуры
    await _ensure_user_anchor(update, context, "menu")
    # если это сообщение от кнопки клавиатуры — удалим его
    if update.effective_message and update.effective_message.text == REPLY_BUTTON_LABEL:
        try:
            await update.effective_message.delete()
        except Exception:
            pass

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ensure_tasks_env(update)
    await _ensure_user_anchor(update, context, "today")
    if update.effective_message and update.effective_message.text:
        try:
            await update.effective_message.delete()
        except Exception:
            pass

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ensure_tasks_env(update)
    await _ensure_user_anchor(update, context, "week")
    if update.effective_message and update.effective_message.text:
        try:
            await update.effective_message.delete()
        except Exception:
            pass

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ensure_tasks_env(update)
    await _ensure_user_anchor(update, context, "month")
    if update.effective_message and update.effective_message.text:
        try:
            await update.effective_message.delete()
        except Exception:
            pass

async def _ensure_tasks_env(update: Update) -> bool:
    if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
        if update.effective_message:
            await update.effective_message.reply_text(
                "❗ Не настроен доступ к Google Tasks. Нужны CLIENT_ID / SECRET / REFRESH_TOKEN / LIST_ID.",
                reply_markup=main_reply_kb(),
                disable_notification=True,
            )
        return False
    return True

# Скрытая команда для теста анонса + ежечасных
async def cmd_test1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await _announce_with_sources(context.application, title, yt_live)
    if update.effective_message:
        try:
            await update.effective_message.reply_text("Тест: отправил анонс и запустил ежечасные напоминания.", disable_notification=True)
        except Exception:
            pass

# ==================== CALLBACKS ====================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    # Определим личный якорь для пользователя
    user_id = q.from_user.id
    chat_id = q.message.chat.id if q.message else None
    if not chat_id:
        return
    key = (chat_id, user_id)
    msg_id = USER_MENU_ANCHORS.get(key)
    # если кликают по чужому сообщению — создадим свой якорь
    if not msg_id:
        fake_update = Update(update.update_id, callback_query=q)
        await _ensure_user_anchor(fake_update, context, "menu")
        msg_id = USER_MENU_ANCHORS.get(key)

    if data == "noop":
        return

    if data == "menu|socials":
        await _render_screen(context, chat_id, msg_id, "socials")
    elif data == "menu|main":
        await _render_screen(context, chat_id, msg_id, "menu")
    elif data == "menu|book":
        await _render_screen(context, chat_id, msg_id, "book")
    elif data == "book|rules":
        await _render_screen(context, chat_id, msg_id, "book_rules")
    elif data.startswith("nav|"):
        parts = data.split("|")
        if parts[1] == "menu":
            await _render_screen(context, chat_id, msg_id, "menu")
        elif parts[1] == "today":
            await _render_screen(context, chat_id, msg_id, "today")
        elif parts[1] == "week":
            await _render_screen(context, chat_id, msg_id, "week")
        elif parts[1] == "month":
            if len(parts) == 2:
                await _render_screen(context, chat_id, msg_id, "month")
            else:
                # nav|month|YYYY-MM|idx
                ym = parts[2]; idx = parts[3]
                await _render_screen(context, chat_id, msg_id, f"month|{ym}|{idx}")

# ==================== ROUTING ====================
async def on_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единственная reply-кнопка — вызвать меню. Сообщение-триггер удаляем."""
    if not update.effective_message or not update.effective_message.text:
        return
    if update.effective_message.text.strip() == REPLY_BUTTON_LABEL:
        await cmd_menu(update, context)

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
async def _on_start(app: Application):
    # Команды (видимые пользователю)
    await app.bot.set_my_commands([
        BotCommand("today", "📅 Стримы сегодня"),
        BotCommand("week", "📅 Стримы на неделю"),
        BotCommand("month", "📅 Стримы за месяц (по неделям)"),
        BotCommand("menu", "Открыть меню"),
        # /test1 НЕ добавляем в список, остаётся «секретной»
    ])

    # Показать клавиатуру и тут же удалить служебное сообщение
    for chat_id in CHAT_IDS:
        try:
            m = await app.bot.send_message(chat_id=chat_id, text="…", reply_markup=main_reply_kb(), disable_notification=True)
            try:
                await app.bot.delete_message(chat_id=chat_id, message_id=m.message_id)
            except Exception:
                pass
        except Exception as e:
            print(f"[STARTED] cannot show keyboard in {chat_id}: {e}")

    # Фоновые задачи
    asyncio.create_task(minute_loop(app))
    asyncio.create_task(daily_reminders_loop(app))
    asyncio.create_task(self_ping())
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")

def main():
    if not TG_TOKEN or not CHAT_IDS:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS in Environment")
    if not PUBLIC_URL:
        raise SystemExit("Set PUBLIC_URL (https://<your-host>) for webhook (или используйте RENDER_EXTERNAL_URL)")

    application = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(_on_start)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week", cmd_week))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("test1", cmd_test1))  # секретная

    # Reply-кнопка
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_buttons))
    # Инлайн-кнопки
    application.add_handler(CallbackQueryHandler(on_callback))

    application.add_error_handler(on_error)

    # Вебхук
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    print(f"[WEBHOOK] listen 0.0.0.0:{PORT}  path={WEBHOOK_PATH}  url={webhook_url}")

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=None,
    )

if __name__ == "__main__":
    main()
