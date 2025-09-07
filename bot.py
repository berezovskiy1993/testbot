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

# Базовый список чатов (старый ENV) — используем как дефолт
_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
CHAT_IDS = [c.strip() for c in _raw_chats.split(",") if c.strip()]

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

# Картинка для постов анонса стрима (фолбэк)
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()

# Картинка для ЕЖЕДНЕВНЫХ напоминаний (легко менять тут)
SCHEDULE_BANNER_URL = "https://ibb.co/LXSMV1FQ"  # <-- можно заменить ссылку здесь

# Соцсети — дефолтные ссылки (можно переопределить через ENV при желании)
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

# ========= ЧАТЫ ДЛЯ ПОСТИНГА (ЛЕГКО ПРАВИТЬ ЗДЕСЬ) =========
# Куда постим анонсы стрима / ежечасные "пинги"
STREAM_POST_CHAT_IDS: list[str] = CHAT_IDS[:]  # можно задать свой список, например ["-100123...", "-100456..."]

# Куда постим ЕЖЕДНЕВНЫЕ напоминания о расписании (10:00, 14:00)
SCHEDULE_REMINDER_CHAT_IDS: list[str] = CHAT_IDS[:]  # можно задать свой список отдельно

# Время для ежедневных напоминаний (локальное)
SCHEDULE_POST_TIMES_LOCAL = ["10:00", "14:00"]  # легко менять

# ========= In-memory state =========
last_twitch_stream_id: str | None = None
_last_yt_video_id: str | None = None  # для ежечасных пингов
_tw_token: str | None = None
_tw_token_expire_at: int = 0  # unix ts
_last_called_ts = {"tw": 0}

# «Якорные» сообщения для меню/расписания (chat_id -> message_id)
_anchor_msg_id: dict[int, int] = {}

# Ежечасные напоминания во время стрима
HOURLY_REMINDER_INTERVAL_SEC = 3600
HOURLY_REMINDER_MAX_HOURS = 6
_live_reminder = {
    "active": False,
    "stream_id": None,
    "started_ts": 0,
    "last_sent_ts": 0,
    "hours_sent": 0,
}

# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ==================== TELEGRAM UI ====================
def build_keyboard(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    yt_url = (
        f"https://www.youtube.com/watch?v={youtube_video_id}"
        if youtube_video_id
        else (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    )
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("❤️ Гоу на YouTube", url=yt_url),
             InlineKeyboardButton("💜 Гоу на Twitch", url=tw_url)],
            [InlineKeyboardButton("💸 Гоу Донатик", url="https://new.donatepay.ru/@Dektrian_TV"),
             InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")],
        ]
    )

def mini_stream_kb(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    """Две кнопки для «ежечасного пинга»."""
    yt_url = (
        f"https://www.youtube.com/watch?v={youtube_video_id}"
        if youtube_video_id
        else (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    )
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ YouTube", url=yt_url),
                                  InlineKeyboardButton("▶️ Twitch", url=tw_url)]])

# Reply-клавиатура (постоянная)
def main_reply_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("📺 Стрим сегодня"), KeyboardButton("📺 Стримы неделя")],
        [KeyboardButton("📺 Стримы месяц"), KeyboardButton("≡ Меню")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, one_time_keyboard=False)

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
    Моноширинное представление (HTML <pre>).
    Пустые даты -> "--" и "нет стримов".
    """
    m = _tasks_by_date_map(tasks)

    lines = []
    header = f"{title}\n"
    lines.append(html_escape(header))
    lines.append("<pre>")
    lines.append("Дата     Дн  Время  Событие")
    lines.append("------- ---- ------ ---------------")
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

def _twitch_get_stream_raw() -> dict | None:
    """Возвращает объект стрима, если сейчас live, иначе None. Не меняет last_twitch_stream_id."""
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET and TWITCH_USERNAME):
        return None
    tk = _tw_fetch_token()
    if not tk:
        return None
    r = requests.get(
        "https://api.twitch.tv/helix/streams",
        params={"user_login": TWITCH_USERNAME},
        headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {tk}"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return data[0] if data else None

def twitch_check_live() -> dict | None:
    """
    Возвращает {'id': stream_id, 'title': title} если обнаружен НОВЫЙ эфир, иначе None.
    """
    global last_twitch_stream_id
    try:
        s = _twitch_get_stream_raw()
        if not s:
            return None
        sid = s.get("id")
        title = s.get("title")
        if sid and sid != last_twitch_stream_id:
            return {"id": sid, "title": title}
        return None
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", "?")
        body = getattr(e.response, "text", "")
        print(f"[TW] streams HTTP {code}: {body}")
    except Exception as e:
        print(f"[TW] error: {e}")
    return None

def twitch_is_live() -> bool:
    try:
        return _twitch_get_stream_raw() is not None
    except Exception:
        return False

# ==================== ОСНОВНАЯ ЛОГИКА ====================
async def tg_broadcast_photo_first(app: Application, text: str, kb: InlineKeyboardMarkup | None, photo_url: str, chat_ids: list[str]):
    """Пробуем фото, иначе сообщение с превью."""
    for chat_id in chat_ids:
        try:
            await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode="HTML", reply_markup=kb)
            continue
        except BadRequest as e:
            print(f"[TG] photo send failed for {chat_id}: {e}. Fallback to link+message.")
        except Exception as e:
            print(f"[TG] photo send error to {chat_id}: {e}. Fallback to link+message.")
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"{photo_url}\n\n{text}",
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=False,
            )
        except Exception as e:
            print(f"[TG] message send error to {chat_id}: {e}")

async def _announce_with_sources(app: Application, title: str, yt_video: dict | None):
    """Финальный анонс старта стрима в каналы/чаты STREAM_POST_CHAT_IDS."""
    global _last_yt_video_id
    yt_id = yt_video["id"] if yt_video else None
    _last_yt_video_id = yt_id
    photo_url = (yt_video.get("thumb") if (yt_video and yt_video.get("thumb")) else STATIC_IMAGE_URL)
    text = (
        "🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{html_escape(title or '')}</b>\n\n"
        "#DEKTRIAN #D13 #ОНЛАЙН"
    )
    kb = build_keyboard(yt_id)
    await tg_broadcast_photo_first(app, text, kb, photo_url, STREAM_POST_CHAT_IDS)

async def _send_hourly_nudge(app: Application):
    """Короткий «мы ещё в эфире»."""
    # попробуем актуализировать YT id, если не знаем
    global _last_yt_video_id
    if not _last_yt_video_id:
        yt_live = await yt_fetch_live_with_retries(max_attempts=1, delay_seconds=0)
        if yt_live:
            _last_yt_video_id = yt_live.get("id")
    text = "Мы всё ещё на стриме, врывайся! 😏"
    kb = mini_stream_kb(_last_yt_video_id)
    for chat_id in STREAM_POST_CHAT_IDS:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        except Exception as e:
            print(f"[NUDGE] send error to {chat_id}: {e}")

async def minute_loop(app: Application):
    """Каждую минуту: проверка Twitch + управление ежечасными пингами + ежедневные напоминания."""
    print(f"[WAKE] minute loop started at {now_local().isoformat()}")
    _last_daily_key = ""
    while True:
        try:
            # 1) Twitch старт?
            if _sec_since(_last_called_ts["tw"]) >= 60:
                print("[WAKE] tick: twitch check")
                tw = twitch_check_live()
                if tw:
                    # новый стрим
                    global last_twitch_stream_id, _live_reminder
                    last_twitch_stream_id = tw["id"]
                    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
                    title = tw.get("title") or (yt_live.get("title") if yt_live else "Стрим")
                    await _announce_with_sources(app, title, yt_live)
                    # включаем ежечасные пинги
                    _live_reminder = {
                        "active": True,
                        "stream_id": last_twitch_stream_id,
                        "started_ts": int(time.time()),
                        "last_sent_ts": int(time.time()),
                        "hours_sent": 0,
                    }
                _last_called_ts["tw"] = int(time.time())

            # 2) Ежечасные пинги, если live
            if _live_reminder["active"]:
                # если эфир закончился — выключаем
                if not twitch_is_live():
                    _live_reminder["active"] = False
                else:
                    if _sec_since(_live_reminder["last_sent_ts"]) >= HOURLY_REMINDER_INTERVAL_SEC and \
                       _live_reminder["hours_sent"] < HOURLY_REMINDER_MAX_HOURS:
                        await _send_hourly_nudge(app)
                        _live_reminder["last_sent_ts"] = int(time.time())
                        _live_reminder["hours_sent"] += 1
                    # авто-стоп после лимита часов
                    if _live_reminder["hours_sent"] >= HOURLY_REMINDER_MAX_HOURS:
                        _live_reminder["active"] = False

            # 3) Ежедневные напоминания (10:00, 14:00 и т.п.) — только если есть задачи на сегодня
            local = now_local()
            key = f"{local.date().isoformat()} {local.strftime('%H:%M')}"
            if key != _last_daily_key and local.strftime("%H:%M") in SCHEDULE_POST_TIMES_LOCAL:
                _last_daily_key = key
                await _try_post_daily_schedule(app)

        except Exception as e:
            print(f"[WAKE] loop error: {e}")

        await asyncio.sleep(5)

async def _try_post_daily_schedule(app: Application):
    """Постит «Стримы сегодня» только если в Google Tasks есть задачи на сегодня."""
    tasks = _tasks_fetch_all()
    today = now_local().date()
    todays = []
    for t in tasks:
        d = _due_to_local_date(t.get("due") or "")
        if d == today:
            todays.append(t)
    if not todays:
        print("[DAILY] no tasks today — skip")
        return

    # Текст без <pre>, обычный
    lines = ["<b>Стримы сегодня:</b>", ""]
    todays_sorted = sorted(
        todays,
        key=lambda t: (_extract_time_from_title(t.get('title') or "")[0] or "99:99"),
    )
    for t in todays_sorted:
        hhmm, title = _extract_time_from_title(t.get("title") or "")
        time_str = hhmm or "--:--"
        lines.append(f"• {time_str} — {html_escape(title)}")
    lines.append("")
    lines.append("Залетайте на стримчики! 🔥")
    text = "\n".join(lines)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")]])

    for chat_id in SCHEDULE_REMINDER_CHAT_IDS:
        try:
            # с картинкой
            await app.bot.send_photo(chat_id=chat_id, photo=SCHEDULE_BANNER_URL, caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            print(f"[DAILY] send error to {chat_id}: {e}")

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

# ==================== «Якорное» сообщение ====================
async def _ensure_anchor(app: Application, chat_id: int) -> int:
    """
    Возвращает message_id якорного сообщения для чата.
    Создаёт его при необходимости.
    """
    mid = _anchor_msg_id.get(chat_id)
    if mid:
        return mid
    # создаём минимальное сообщение и сохраняем id
    try:
        msg = await app.bot.send_message(chat_id=chat_id, text="Меню бота:", reply_markup=_main_menu_kb())
        _anchor_msg_id[chat_id] = msg.message_id
        return msg.message_id
    except Exception as e:
        print(f"[ANCHOR] create failed in {chat_id}: {e}")
        raise

async def _edit_anchor(app: Application, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None):
    """Правит якорное сообщение, создавая его при необходимости."""
    mid = await _ensure_anchor(app, chat_id)
    try:
        await app.bot.edit_message_text(chat_id=chat_id, message_id=mid, text=text, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        # если «message is not modified» — обновим только клавиатуру
        if "message is not modified" in str(e).lower():
            await app.bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=reply_markup)
        else:
            raise

# ==================== КОМАНДЫ РАСПИСАНИЯ (через якорь) ====================
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    chat_id = update.effective_chat.id
    tasks = _tasks_fetch_all()
    d = now_local().date()
    text = _format_table_for_range(tasks, d, d, f"📅 Сегодня — {d.strftime('%d.%m.%Y')}")
    await _edit_anchor(context.application, chat_id, text, _nav_kb_for("today"))

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    chat_id = update.effective_chat.id
    tasks = _tasks_fetch_all()
    start = now_local().date()
    end = start + timedelta(days=6)
    text = _format_table_for_range(tasks, start, end, f"📅 Неделя — {start.strftime('%d.%m')}–{end.strftime('%d.%m')}")
    await _edit_anchor(context.application, chat_id, text, _nav_kb_for("week"))

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
    return f"📅 {ru_months[month]} {year} — Неделя {idx+1}/{total}"

def _month_kb(ym: str, idx: int, total: int) -> InlineKeyboardMarkup:
    prev_idx = (idx - 1) % total
    next_idx = (idx + 1) % total
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️", callback_data=f"m|{ym}|{prev_idx}"),
         InlineKeyboardButton(f"Неделя {idx+1}/{total}", callback_data=f"m|{ym}|{idx}"),
         InlineKeyboardButton("▶️", callback_data=f"m|{ym}|{next_idx}")],
        [_back_to_menu_btn()],
    ])

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    chat_id = update.effective_chat.id
    today = now_local().date()
    year, month = today.year, today.month
    weeks = _month_weeks(year, month)
    idx = 0
    tasks = _tasks_fetch_all()
    start, end = weeks[idx]
    text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
    kb = _month_kb(f"{year:04d}-{month:02d}", idx, len(weeks))
    await _edit_anchor(context.application, chat_id, text, kb)

async def on_month_nav(query_data: str, query, context: ContextTypes.DEFAULT_TYPE):
    try:
        _, ym, idx_str = query_data.split("|")
        year, month = map(int, ym.split("-"))
        idx = int(idx_str)
    except Exception:
        return
    tasks = _tasks_fetch_all()
    weeks = _month_weeks(year, month)
    if not weeks:
        return
    idx = max(0, min(idx, len(weeks)-1))
    start, end = weeks[idx]
    text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
    kb = _month_kb(ym, idx, len(weeks))
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except BadRequest:
        await query.edit_message_reply_markup(reply_markup=kb)

# ==================== ИНЛАЙН-МЕНЮ ====================
def _back_to_menu_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton("← В меню", callback_data="menu|main")

def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
         InlineKeyboardButton("📅 Неделя", callback_data="t|week")],
        [InlineKeyboardButton("📅 Месяц", callback_data="t|month"),
         InlineKeyboardButton("Бронь стрима", callback_data="menu|booking")],
        [InlineKeyboardButton("Купить юси", url="https://t.me/uc_pubg_bounty"),
         InlineKeyboardButton("Вступить в клан", url="https://t.me/D13_join_bot")],
        [InlineKeyboardButton("Соцсети стримера", callback_data="menu|socials"),
         InlineKeyboardButton("← Закрыть меню", callback_data="menu|close")],
    ])

def _socials_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("YouTube", url=SOC_YT),
         InlineKeyboardButton("Twitch", url=SOC_TWITCH)],
        [InlineKeyboardButton("Группа Telegram", url=SOC_TG_GROUP),
         InlineKeyboardButton("Канал Telegram", url=SOC_TG_CHANNEL)],
        [InlineKeyboardButton("TikTok", url=SOC_TIKTOK)],
        [_back_to_menu_btn(), InlineKeyboardButton("Закрыть", callback_data="menu|close")],
    ]
    return InlineKeyboardMarkup(rows)

def _booking_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Условия брони", callback_data="booking|rules"),
         InlineKeyboardButton("Сделать бронь", url="https://t.me/DektrianTV")],
        [_back_to_menu_btn(), InlineKeyboardButton("Закрыть", callback_data="menu|close")],
    ])

def _booking_rules_text() -> str:
    return (
        "<b>Условия бронирования:</b>\n\n"
        "• Призовые кастомки — <b>бесплатно</b>, при условии от 3 игр.\n"
        "  Приз: <b>480 UC</b> за карту, свободный вход.\n"
        "• Турниры / лиги / праки — от <b>250₽ / 125₴</b> за 1 катку (по договорённости).\n"
        "• TDM-турниры — от <b>100₽ / 50₴</b> за катку.\n\n"
        "По деталям и брони — жми «Сделать бронь». 🔥"
    )

def _nav_kb_for(where: str) -> InlineKeyboardMarkup:
    # Небольшая навигация под таблицами
    if where == "today":
        rows = [[InlineKeyboardButton("📅 Неделя", callback_data="t|week"),
                 InlineKeyboardButton("📅 Месяц", callback_data="t|month")],
                [_back_to_menu_btn()]]
    elif where == "week":
        rows = [[InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
                 InlineKeyboardButton("📅 Месяц", callback_data="t|month")],
                [_back_to_menu_btn()]]
    else:
        rows = [[InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
                 InlineKeyboardButton("📅 Неделя", callback_data="t|week")],
                [_back_to_menu_btn()]]
    return InlineKeyboardMarkup(rows)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await _edit_anchor(context.application, chat_id, "Меню бота:", _main_menu_kb())

async def on_menu_nav(query_data: str, query, context: ContextTypes.DEFAULT_TYPE):
    chat_id = query.message.chat_id
    if query_data == "menu|socials":
        await query.edit_message_text("Соцсети стримера:", reply_markup=_socials_kb())
    elif query_data == "menu|booking":
        await query.edit_message_text("Бронь стрима:", reply_markup=_booking_kb())
    elif query_data == "booking|rules":
        await query.edit_message_text(_booking_rules_text(), parse_mode="HTML", reply_markup=_booking_kb())
    elif query_data == "menu|main":
        await query.edit_message_text("Меню бота:", reply_markup=_main_menu_kb())
    elif query_data == "menu|close":
        # Просто уберём клавиатуру, текст оставим
        await query.edit_message_reply_markup(reply_markup=None)

# ==================== HANDLERS: reply-клавиатура ====================
# Жёсткие совпадения текста — чтобы не реагировать на обычные слова
KB_LABELS = {
    "today": "📺 Стрим сегодня",
    "week": "📺 Стримы неделя",
    "month": "📺 Стримы месяц",
    "menu": "≡ Меню",
}

async def on_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_message.text:
        return
    txt = update.effective_message.text.strip()
    if txt == KB_LABELS["today"]:
        await cmd_today(update, context)
    elif txt == KB_LABELS["week"]:
        await cmd_week(update, context)
    elif txt == KB_LABELS["month"]:
        await cmd_month(update, context)
    elif txt == KB_LABELS["menu"]:
        await cmd_menu(update, context)

# ==================== КОМАНДЫ: тест анонса (имитация старта) ====================
async def cmd_test1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await _announce_with_sources(context.application, title, yt_live)
    # включим «ежечасные» как при реальном старте
    global _live_reminder
    _live_reminder = {
        "active": True,
        "stream_id": f"simulated-{int(time.time())}",
        "started_ts": int(time.time()),
        "last_sent_ts": int(time.time()),
        "hours_sent": 0,
    }
    if update.effective_message:
        await update.effective_message.reply_text("Тест: анонс отправлен, пинги запущены.", reply_markup=main_reply_kb())

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
async def _activate_keyboard_once(app: Application):
    """Тихо активируем reply-клавиатуру: отправили — удалили."""
    target_chats = sorted(set(STREAM_POST_CHAT_IDS + SCHEDULE_REMINDER_CHAT_IDS))
    for chat_id in target_chats:
        try:
            msg = await app.bot.send_message(chat_id=chat_id, text="Клавиатура:", reply_markup=main_reply_kb())
            # удалим, чтобы не шуметь
            try:
                await app.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
            except Exception:
                pass
        except Exception as e:
            print(f"[STARTUP] keyboard activate failed in {chat_id}: {e}")

async def _on_start(app: Application):
    # Команды (только латиница)
    await app.bot.set_my_commands([
        BotCommand("today", "📅 Стримы сегодня"),
        BotCommand("week", "🗓 Стримы на неделю"),
        BotCommand("month", "📆 Стримы за месяц (по неделям)"),
        BotCommand("menu", "Открыть меню"),
        BotCommand("test1", "Тест анонса (для владельца)"),
    ])

    # Тихая активация клавиатуры
    await _activate_keyboard_once(app)

    # Фоновые задачи
    asyncio.create_task(minute_loop(app))
    asyncio.create_task(self_ping())
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")

# ==================== ROUTING (callbacks) ====================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    if data.startswith("m|"):
        await on_month_nav(data, q, context)
    elif data.startswith("menu|") or data.startswith("booking|"):
        await on_menu_nav(data, q, context)
    elif data.startswith("t|"):
        action = data.split("|", 1)[1]
        dummy_update = Update(update.update_id, message=q.message)
        if action == "today":
            await cmd_today(dummy_update, context)
        elif action == "week":
            await cmd_week(dummy_update, context)
        elif action == "month":
            await cmd_month(dummy_update, context)

# ==================== HELPERS ====================
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

# ==================== MAIN ====================
def main():
    if not TG_TOKEN or not (STREAM_POST_CHAT_IDS or SCHEDULE_REMINDER_CHAT_IDS):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN и проверьте списки чатов в коде (STREAM_POST_CHAT_IDS / SCHEDULE_REMINDER_CHAT_IDS).")
    if not PUBLIC_URL:
        raise SystemExit("Set PUBLIC_URL (https://<your-host>) for webhook (или используйте RENDER_EXTERNAL_URL)")

    application = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(_on_start)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week", cmd_week))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("test1", cmd_test1))

    # Reply-клавиатура (только точные совпадения меток)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_buttons))

    # Callback-кнопки
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
