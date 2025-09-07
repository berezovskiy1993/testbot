import os
import time
import asyncio
import re
import calendar
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

# Базовые (общие) чаты из ENV: закрытые каналы/чаты ID формата -100...
_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
CHAT_IDS_ENV = [int(c.strip()) for c in _raw_chats.split(",") if c.strip()]

# Киев: летом UTC+3, зимой UTC+2 — вручную
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# === Google Tasks (для расписания) ===
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
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()  # fallback для анонса
REMINDER_IMAGE_URL = os.getenv("REMINDER_IMAGE_URL", "https://ibb.co/LXSMV1FQ").strip()  # картинка для напоминаний

# Соцсети (для меню «Соцсети»)
SOC_YT = os.getenv("SOCIAL_YOUTUBE", "").strip()
SOC_TWITCH = os.getenv("SOCIAL_TWITCH", "").strip()
SOC_TG_GROUP = os.getenv("SOCIAL_TG_GROUP", "https://t.me/dektrian_tv").strip()
SOC_TG_CHANNEL = os.getenv("SOCIAL_TG_CHANNEL", "https://t.me/dektrian_family").strip()
SOC_TIKTOK = os.getenv("SOCIAL_TIKTOK", "https://www.tiktok.com/@dektrian_tv").strip()

# Вебхук
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "8080")))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dektrian-secret")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", f"/telegram/{BOT_NAME}")

# ========= РАЗДЕЛЕНИЕ КАНАЛОВ ПО РОЛЯМ (правь прямо здесь) =========
# Куда шлём анонсы «стрим начался» и почасовые «мы ещё в эфире»
STREAM_POST_CHATS: list[int] = [
    # Пример: -1001234567890,
]
# Куда шлём ежедневные напоминания по расписанию из Google Tasks
REMINDER_POST_CHATS: list[int] = [
    # Пример: -1009876543210,
]
# Если списки пустые — используем CHAT_IDS_ENV
def _targets_for_stream_posts() -> list[int]:
    return STREAM_POST_CHATS or CHAT_IDS_ENV
def _targets_for_reminders() -> list[int]:
    return REMINDER_POST_CHATS or CHAT_IDS_ENV

# ========= In-memory state =========
last_twitch_stream_id: str | None = None
_tw_token: str | None = None
_tw_token_expire_at: int = 0  # unix ts
_last_called_ts = {"tw": 0}

# Якорные сообщения
LAST_ANCHOR: dict[int, int] = {}  # chat_id -> message_id

# Флаг текущего эфира + таск почасовых пингов
IS_LIVE: bool = False
_live_pulse_task: asyncio.Task | None = None
LIVE_PULSE_EVERY_MIN = 60  # раз в час

# Напоминалки: локальные "будильники" в формате HH:MM (локальное время Киева)
REMINDER_TIMES: list[str] = ["10:00", "14:00"]
_SENT_REMINDER_KEYS: set[str] = set()  # "YYYY-MM-DD_HH:MM"


# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def _delete_if_possible(bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def _delete_user_trigger(update: Update):
    if update and update.effective_message:
        try:
            await update.effective_message.delete()
        except Exception:
            pass

async def _replace_anchor(app: Application, chat_id: int, text: str, kb: InlineKeyboardMarkup | None, parse_mode: str = "HTML"):
    """
    Удаляем предыдущий якорь, отправляем новый БЕЗ ЗВУКА, сохраняем его id.
    """
    old_id = LAST_ANCHOR.get(chat_id)
    if old_id:
        await _delete_if_possible(app.bot, chat_id, old_id)
    msg = await app.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=kb,
        disable_web_page_preview=False,
        disable_notification=True,  # <<< без звука
    )
    LAST_ANCHOR[chat_id] = msg.message_id
    return msg.message_id


# ==================== TELEGRAM UI ====================
# Тексты кнопок ReplyKeyboard (строго по равенству)
LABEL_TODAY = "📅 Стримы сегодня"
LABEL_WEEK = "📅 Стримы на неделю"
LABEL_MONTH = "📅 Стримы за месяц"
LABEL_MENU = "☰ Меню"

def main_reply_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(LABEL_TODAY), KeyboardButton(LABEL_WEEK)],
        [KeyboardButton(LABEL_MONTH), KeyboardButton(LABEL_MENU)],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, one_time_keyboard=False)

def _tabs_kb(selected: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
            InlineKeyboardButton("📅 Неделя", callback_data="t|week"),
            InlineKeyboardButton("📅 Месяц", callback_data="t|month"),
        ],
        [InlineKeyboardButton("← Меню", callback_data="menu|main")],
    ])

def _combine_kb_rows(*markups: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for m in markups:
        if not m:
            continue
        rows.extend(m.inline_keyboard)
    return InlineKeyboardMarkup(rows)

def _main_menu_kb() -> InlineKeyboardMarkup:
    # Два столбца
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
         InlineKeyboardButton("📅 Неделя", callback_data="t|week")],
        [InlineKeyboardButton("📅 Месяц", callback_data="t|month"),
         InlineKeyboardButton("Соцсети стримера", callback_data="menu|socials")],
        [InlineKeyboardButton("Бронь стрима", callback_data="menu|book"),
         InlineKeyboardButton("Вступить в клан", url="https://t.me/D13_join_bot")],
    ])

def _socials_kb() -> InlineKeyboardMarkup:
    yt = SOC_YT or (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    tw = SOC_TWITCH or (f"https://www.twitch.tv/{TWITCH_USERNAME}" if TWITCH_USERNAME else "https://www.twitch.tv/")
    rows = [
        [InlineKeyboardButton("YouTube", url=yt),
         InlineKeyboardButton("Twitch", url=tw)],
        [InlineKeyboardButton("Группа Telegram", url=SOC_TG_GROUP),
         InlineKeyboardButton("Канал Telegram", url=SOC_TG_CHANNEL)],
        [InlineKeyboardButton("TikTok", url=SOC_TIKTOK)],
        [InlineKeyboardButton("← Назад", callback_data="menu|main")],
    ]
    return InlineKeyboardMarkup(rows)

def _book_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Условия брони", callback_data="book|info")],
        [InlineKeyboardButton("Сделать бронь", url="https://t.me/DektrianTV")],
        [InlineKeyboardButton("← Назад", callback_data="menu|main")],
    ])

BOOK_INFO_TEXT = (
    "📌 <b>Условия бронирования</b>\n"
    "• Призовые кастомки — <b>бесплатно</b> при условии от 3-х игр, приз за карту — <b>480 UC</b>, вход свободный.\n"
    "• Турниры / лиги / праки — от <b>250₽ / 125₴</b> за 1 катку (по договорённости).\n"
    "• TDM турниры — от <b>100₽ / 50₴</b> за 1 катку.\n"
    "\nНаписать мне в ЛС: @DektrianTV"
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
    t = _mention_re.sub("", title)  # убираем @юзернеймы
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

def _daterange_days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

def _tasks_by_date_map(tasks: list[dict]) -> dict[date, list[dict]]:
    out: dict[date, list[dict]] = {}
    for t in tasks:
        d = _due_to_local_date(t.get("due") or "")
        if not d:
            continue
        out.setdefault(d, []).append(t)
    return out

def _weekday_abr(d: date) -> str:
    return d.strftime("%a")

def _format_table_for_range(tasks: list[dict], start: date, end: date, title: str) -> str:
    """
    Монотаблица. Пустые даты → '--' + 'нет стримов'. Без сокращения слов.
    """
    m = _tasks_by_date_map(tasks)
    lines = []
    header = f"{title}\n"
    lines.append(html_escape(header))
    lines.append("<pre>")
    lines.append("Дата     Дн  Время  Событие")
    lines.append("------- ---- ------ ------------------------------")
    for d in _daterange_days(start, end):
        day = d.strftime("%d.%m")
        wd = _weekday_abr(d)
        day_tasks = m.get(d, [])
        if not day_tasks:
            lines.append(f"{day:8} {wd:3} {'--':5}  нет стримов")
            continue
        day_tasks_sorted = sorted(
            day_tasks,
            key=lambda t: (_extract_time_from_title(t.get('title') or "")[0] or "99:99")
        )
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

def twitch_check_live() -> dict | None:
    """
    Возвращает {'id': stream_id, 'title': title} если обнаружен НОВЫЙ эфир, иначе None.
    """
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
            print(f"[TW] streams HTTP {e.response.status_code}: retry with fresh token")
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
        code = getattr(e.response, "status_code", "?")
        body = getattr(e.response, "text", "")
        print(f"[TW] streams HTTP {code}: {body}")
    except Exception as e:
        print(f"[TW] error: {e}")
    return None


# ==================== ЛОГИКА ПОСТИНГА ====================
def _live_buttons(yt_video_id: str | None) -> InlineKeyboardMarkup:
    yt_url = (
        f"https://www.youtube.com/watch?v={yt_video_id}"
        if yt_video_id else
        (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    )
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ YouTube", url=yt_url),
         InlineKeyboardButton("💜 Twitch",  url=tw_url)],
    ])

def _join_only_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")]])

async def tg_broadcast_photo_first(app: Application, text: str, kb: InlineKeyboardMarkup | None, photo_url: str, targets: list[int] | None = None):
    """
    Сначала фото (баннер). Если не вышло, — сообщение с ссылкой и превью.
    """
    targets = targets or CHAT_IDS_ENV
    for chat_id in targets:
        # 1) Пытаемся фото
        try:
            await app.bot.send_photo(
                chat_id=chat_id,
                photo=photo_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb
            )
            continue
        except BadRequest as e:
            print(f"[TG] photo send failed for {chat_id}: {e}. Fallback to link+message.")
        except Exception as e:
            print(f"[TG] photo send error to {chat_id}: {e}. Fallback to link+message.")
        # 2) Фолбэк
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"{photo_url}\n\n{text}",
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=False
            )
        except Exception as e:
            print(f"[TG] message send error to {chat_id}: {e}")

async def _announce_with_sources(app: Application, title: str, yt_video: dict | None):
    yt_id = yt_video["id"] if yt_video else None
    photo_url = (yt_video.get("thumb") if (yt_video and yt_video.get("thumb")) else STATIC_IMAGE_URL)
    text = (
        "🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{html_escape(title or '')}</b>\n\n"
        "#DEKTRIAN #D13 #ОНЛАЙН"
    )
    await tg_broadcast_photo_first(app, text, _live_buttons(yt_id), photo_url, targets=_targets_for_stream_posts())

async def _send_live_pulse(app: Application):
    # Короткий «мы ещё в эфире»
    yt_live = await yt_fetch_live_with_retries(max_attempts=1, delay_seconds=0)
    yt_id = yt_live["id"] if yt_live else None
    text = "⏱ Мы всё ещё на стриме — врывайся! 😏"
    await tg_broadcast_photo_first(app, text, _live_buttons(yt_id), photo_url=STATIC_IMAGE_URL, targets=_targets_for_stream_posts())

async def _live_pulse_loop(app: Application):
    try:
        while IS_LIVE:
            await asyncio.sleep(LIVE_PULSE_EVERY_MIN * 60)
            if IS_LIVE:
                await _send_live_pulse(app)
    except asyncio.CancelledError:
        pass

def _start_live_pulses(app: Application):
    global _live_pulse_task
    if _live_pulse_task and not _live_pulse_task.done():
        _live_pulse_task.cancel()
    _live_pulse_task = asyncio.create_task(_live_pulse_loop(app))

def _stop_live_pulses():
    global _live_pulse_task
    if _live_pulse_task and not _live_pulse_task.done():
        _live_pulse_task.cancel()
    _live_pulse_task = None


# ==================== ФОН: Твич и напоминалки ====================
async def minute_loop(app: Application):
    """
    1) Раз в ~минуту — проверяем Twitch.
    2) Каждую минуту — смотрим, не пора ли отправить напоминание по задачам.
    """
    print(f"[WAKE] minute loop started at {now_local().isoformat()}")
    while True:
        try:
            # -- Twitch
            if _sec_since(_last_called_ts["tw"]) >= 60:
                tw = twitch_check_live()
                if tw:
                    # Новый эфир
                    global IS_LIVE
                    IS_LIVE = True
                    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
                    title = tw.get("title") or (yt_live.get("title") if yt_live else "Стрим")
                    await _announce_with_sources(app, title, yt_live)
                    _start_live_pulses(app)
                else:
                    # Эфир закончился?
                    if IS_LIVE:
                        IS_LIVE = False
                        _stop_live_pulses()
                _last_called_ts["tw"] = int(time.time())

            # -- Reminders by local time
            await _check_reminders(app)

        except Exception as e:
            print(f"[WAKE] loop error: {e}")
        await asyncio.sleep(5)

async def _check_reminders(app: Application):
    """
    В REMINDER_TIMES по Киеву шлём пост на сегодня,
    только если на сегодня есть стримы. Картинка REMINDER_IMAGE_URL.
    """
    local_now = now_local()
    hhmm = local_now.strftime("%H:%M")
    if hhmm not in REMINDER_TIMES:
        return
    key = f"{local_now.date().isoformat()}_{hhmm}"
    if key in _SENT_REMINDER_KEYS:
        return

    tasks = _tasks_fetch_all()
    today = local_now.date()
    todays = [t for t in tasks if _due_to_local_date(t.get("due") or "") == today]
    if not todays:
        _SENT_REMINDER_KEYS.add(key)
        return

    # Формируем обычный текст (НЕ моно), без лишних кнопок, только «вступить в клан»
    lines = [
        "📢 <b>Стримы сегодня</b>",
        "",
    ]
    # Сортировка
    def sort_key(t: dict):
        d = _due_to_local_date(t.get("due") or "")
        time_in_title, _ = _extract_time_from_title(t.get("title") or "")
        time_sort = time_in_title or "99:99"
        return (d or datetime(2100, 1, 1).date(), time_sort)
    todays_sorted = sorted(todays, key=sort_key)

    for t in todays_sorted:
        hhmm, cleaned = _extract_time_from_title(t.get("title") or "")
        if hhmm:
            lines.append(f"• {hhmm} — {html_escape(cleaned)}")
        else:
            lines.append(f"• {html_escape(cleaned)}")
    lines.append("")
    lines.append("Залетай на стримчики! 🔥")

    text = "\n".join(lines)
    await tg_broadcast_photo_first(app, text, _join_only_kb(), photo_url=REMINDER_IMAGE_URL, targets=_targets_for_reminders())
    _SENT_REMINDER_KEYS.add(key)


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
                    print(f"[SELF-PING] status={resp.status}")
        except Exception as e:
            print(f"[SELF-PING] error: {e}")
        await asyncio.sleep(600)


# ==================== КОМАНДЫ: расписание и меню ====================
async def _ensure_tasks_env(update: Update) -> bool:
    if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
        if update.effective_message:
            await update.effective_message.reply_text(
                "❗ Не настроен доступ к Google Tasks. "
                "Нужны GOOGLE_TASKS_CLIENT_ID / SECRET / REFRESH_TOKEN / LIST_ID в ENV.",
                reply_markup=main_reply_kb(),
            )
        return False
    return True

def _month_weeks(year: int, month: int) -> list[tuple[date, date]]:
    last_day = calendar.monthrange(year, month)[1]
    weeks = []
    d = date(year, month, 1)
    while d.month == month:
        start = d
        end = min(date(year, month, last_day), start + timedelta(days=6))
        weeks.append((start, end))
        d = end + timedelta(days=1)
    return weeks  # 4-5 недель

def _month_title(year: int, month: int, idx: int, total: int) -> str:
    ru_months = ["", "Январь","Февраль","Март","Апрель","Май","Июнь","Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    return f"📆 {ru_months[month]} {year} — Неделя {idx+1}/{total}"

def _month_kb(ym: str, idx: int, total: int) -> InlineKeyboardMarkup:
    prev_idx = (idx - 1) % total
    next_idx = (idx + 1) % total
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️", callback_data=f"m|{ym}|{prev_idx}"),
         InlineKeyboardButton(f"Неделя {idx+1}/{total}", callback_data=f"m|{ym}|{idx}"),
         InlineKeyboardButton("▶️", callback_data=f"m|{ym}|{next_idx}")]
    ])

async def _build_today_text() -> str:
    tasks = _tasks_fetch_all()
    d = now_local().date()
    return _format_table_for_range(tasks, d, d, f"📅 Сегодня — {d.strftime('%d.%m.%Y')}")

async def _build_week_text() -> str:
    tasks = _tasks_fetch_all()
    start = now_local().date()
    end = start + timedelta(days=6)
    return _format_table_for_range(tasks, start, end, f"📅 Неделя — {start.strftime('%d.%m')}–{end.strftime('%d.%m')}")

async def _build_month_text(idx: int | None = 0) -> tuple[str, InlineKeyboardMarkup]:
    tasks = _tasks_fetch_all()
    today = now_local().date()
    year, month = today.year, today.month
    weeks = _month_weeks(year, month)
    if not weeks:
        return "Нет данных по месяцу.", _tabs_kb("month")
    idx = max(0, min(idx or 0, len(weeks)-1))
    start, end = weeks[idx]
    text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
    nav = _month_kb(f"{year:04d}-{month:02d}", idx, len(weeks))
    kb = _combine_kb_rows(nav, _tabs_kb("month"))
    return text, kb

# --- Команды: удаляем триггер, создаём новый якорь (без звука) ---
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_user_trigger(update)
    chat_id = update.effective_chat.id
    await _replace_anchor(context.application, chat_id, "Меню бота:", _main_menu_kb())

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update): return
    await _delete_user_trigger(update)
    chat_id = update.effective_chat.id
    text = await _build_today_text()
    kb = _tabs_kb("today")
    await _replace_anchor(context.application, chat_id, text, kb)

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update): return
    await _delete_user_trigger(update)
    chat_id = update.effective_chat.id
    text = await _build_week_text()
    kb = _tabs_kb("week")
    await _replace_anchor(context.application, chat_id, text, kb)

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update): return
    await _delete_user_trigger(update)
    chat_id = update.effective_chat.id
    text, kb = await _build_month_text(idx=0)
    await _replace_anchor(context.application, chat_id, text, kb)


# ==================== КОМАНДЫ: тест анонса ====================
async def cmd_test1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Имитация старта стрима: берём превью YouTube с 3 попыток; если нет — статичную картинку.
    Отправляем анонс в целевые каналы как при реальном онлайне.
    """
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await _announce_with_sources(context.application, title, yt_live)
    await _delete_user_trigger(update)


# ==================== ROUTING ====================
async def on_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатываем только точные нажатия кнопок ReplyKeyboard — никаких подстрок.
    Каждый такой запрос создаёт новое (тихое) якорное сообщение и удаляет старое.
    """
    if not update.effective_message or not update.effective_message.text:
        return
    txt = update.effective_message.text.strip()
    if txt == LABEL_TODAY:
        await cmd_today(update, context)
    elif txt == LABEL_WEEK:
        await cmd_week(update, context)
    elif txt == LABEL_MONTH:
        await cmd_month(update, context)
    elif txt == LABEL_MENU:
        await cmd_menu(update, context)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    # t|today/week/month — переключатели табов (редактируем существующий якорь)
    if data.startswith("t|"):
        action = data.split("|", 1)[1]
        try:
            if action == "today":
                if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
                    await q.edit_message_text("❗ Google Tasks не настроен.", reply_markup=_tabs_kb())
                else:
                    text = await _build_today_text()
                    await q.edit_message_text(text, parse_mode="HTML", reply_markup=_tabs_kb("today"))
            elif action == "week":
                if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
                    await q.edit_message_text("❗ Google Tasks не настроен.", reply_markup=_tabs_kb())
                else:
                    text = await _build_week_text()
                    await q.edit_message_text(text, parse_mode="HTML", reply_markup=_tabs_kb("week"))
            elif action == "month":
                if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
                    await q.edit_message_text("❗ Google Tasks не настроен.", reply_markup=_tabs_kb())
                else:
                    text, kb = await _build_month_text(idx=0)
                    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except BadRequest:
            try:
                if action == "month":
                    text, kb = await _build_month_text(idx=0)
                    await q.edit_message_reply_markup(reply_markup=kb)
                else:
                    await q.edit_message_reply_markup(reply_markup=_tabs_kb(action))
            except Exception:
                pass
        return

    # m|YYYY-MM|idx — навигация по неделям месяца
    if data.startswith("m|"):
        try:
            _, ym, idx_str = data.split("|")
            year, month = map(int, ym.split("-"))
            idx = int(idx_str)
        except Exception:
            return
        try:
            tasks = _tasks_fetch_all()
            weeks = _month_weeks(year, month)
            if not weeks:
                return
            idx = max(0, min(idx, len(weeks)-1))
            start, end = weeks[idx]
            text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
            nav = _month_kb(ym, idx, len(weeks))
            kb = _combine_kb_rows(nav, _tabs_kb("month"))
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except BadRequest:
            try:
                nav = _month_kb(ym, idx, len(weeks))
                kb = _combine_kb_rows(nav, _tabs_kb("month"))
                await q.edit_message_reply_markup(reply_markup=kb)
            except Exception:
                pass
        return

    # Меню
    if data.startswith("menu|"):
        key = data.split("|", 1)[1]
        if key == "main":
            try:
                await q.edit_message_text("Меню бота:", reply_markup=_main_menu_kb())
            except BadRequest:
                await q.edit_message_reply_markup(reply_markup=_main_menu_kb())
        elif key == "socials":
            try:
                await q.edit_message_text("Соцсети стримера:", reply_markup=_socials_kb())
            except BadRequest:
                await q.edit_message_reply_markup(reply_markup=_socials_kb())
        elif key == "book":
            try:
                await q.edit_message_text("Бронь стрима:", reply_markup=_book_kb())
            except BadRequest:
                await q.edit_message_reply_markup(reply_markup=_book_kb())
        return

    # Бронь
    if data.startswith("book|"):
        key = data.split("|", 1)[1]
        if key == "info":
            try:
                await q.edit_message_text(BOOK_INFO_TEXT, parse_mode="HTML", reply_markup=_book_kb())
            except BadRequest:
                await q.edit_message_reply_markup(reply_markup=_book_kb())
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
async def _on_start(app: Application):
    # Команды (видимы всем; тестовую не публикуем)
    await app.bot.set_my_commands([
        BotCommand("today", "📅 Стримы сегодня"),
        BotCommand("week", "📅 Стримы на неделю"),
        BotCommand("month", "📅 Стримы за месяц"),
        BotCommand("menu", "Открыть меню"),
    ])
    # Никаких стартовых сообщений — молчим, пока нас не вызовут.
    asyncio.create_task(minute_loop(app))
    asyncio.create_task(self_ping())
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")


# ==================== MAIN ====================
def main():
    if not TG_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in ENV")
    if not (STREAM_POST_CHATS or REMINDER_POST_CHATS or CHAT_IDS_ENV):
        raise SystemExit("Add chat IDs: STREAM_POST_CHATS / REMINDER_POST_CHATS (in code) or TELEGRAM_CHAT_IDS in ENV")
    if not PUBLIC_URL:
        raise SystemExit("Set PUBLIC_URL (https://<your-host>) for webhook (или используйте RENDER_EXTERNAL_URL)")

    application = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(_on_start)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("test1", cmd_test1))  # скрытая
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week", cmd_week))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("menu", cmd_menu))

    # Текстовые кнопки (ReplyKeyboard) — только точные совпадения
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_buttons))

    # Callback-кнопки (InlineKeyboard)
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
