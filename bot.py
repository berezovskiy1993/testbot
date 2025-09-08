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

# Базовый список чатов/каналов (ID формата -100...)
_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
CHAT_IDS = [c.strip() for c in _raw_chats.split(",") if c.strip()]

# Киев: летом UTC+3, зимой UTC+2 — вручную
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# === Google Tasks (для расписания) ===
GOOGLE_TASKS_CLIENT_ID = os.getenv("GOOGLE_TASKS_CLIENT_ID", "").strip()
GOOGLE_TASKS_CLIENT_SECRET = os.getenv("GOOGLE_TASKS_CLIENT_SECRET", "").strip()
GOOGLE_TASKS_REFRESH_TOKEN = os.getenv("GOOGLE_TASKS_REFRESH_TOKEN", "").strip()
GOOGLE_TASKS_LIST_ID = os.getenv("GOOGLE_TASKS_LIST_ID", "").strip()

# YouTube (для превью в анонсе старта Twitch)
YT_API_KEY = os.getenv("YT_API_KEY", "").strip()
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "").strip()

# Twitch
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME", "dektrian_tv").strip()

# Картинки
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()  # для анонса старта эфира
SCHEDULE_IMAGE_URL = os.getenv("SCHEDULE_IMAGE_URL", "https://ibb.co/LXSMV1FQ").strip()  # для дневных напоминаний

# Соцсети (дефолты, можно менять в коде/ENV)
SOC_YT = os.getenv("SOCIAL_YOUTUBE", "").strip()
SOC_TWITCH = os.getenv("SOCIAL_TWITCH", "").strip()
SOC_TG_GROUP = os.getenv("SOCIAL_TG_GROUP", "https://t.me/dektrian_tv").strip()
SOC_TG_CHANNEL = os.getenv("SOCIAL_TG_CHANNEL", "https://t.me/dektrian_family").strip()
SOC_TIKTOK = os.getenv("SOCIAL_TIKTOK", "https://www.tiktok.com/@dektrian_tv").strip()

# Параметры вебхука
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "8080")))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dektrian-secret")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", f"/telegram/{BOT_NAME}")

# ========= Настройки постинга (редактируй при желании прямо в коде) =========
# Куда постим объявления о старте стрима и почасовые напоминания:
STREAM_CHAT_IDS = CHAT_IDS  # например: ["-100123...", "-100456..."]

# Куда постим дневные напоминалки по расписанию (Google Tasks):
SCHEDULE_CHAT_IDS = CHAT_IDS  # можно задать отдельно от STREAM_CHAT_IDS

# Время ежедневных напоминаний (локальное, Europe/Kyiv), формат "HH:MM"
DAILY_REMIND_AT = ["10:00", "14:00"]

# Интервал почасовых напоминаний в минутах (во время активного эфира Twitch)
LIVE_REMIND_EVERY_MIN = 60

# ========= In-memory state =========
last_twitch_stream_id: str | None = None
_tw_token: str | None = None
_tw_token_expire_at: int = 0  # unix ts
_last_called_ts = {"tw": 0}

# Якоря сообщений (чтобы удалять предыдущие и держать чат чистым)
ANCHOR_BY_CHAT: dict[str, int] = {}  # chat_id -> message_id
LAST_HOURLY_MSG_ID: dict[str, int] = {}  # для напоминаний во время эфира
HOURLY_TASK: asyncio.Task | None = None

# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ==================== TELEGRAM UI ====================
KB_MAIN_LABEL = "📋 Расписание стримов и прочее"

def main_reply_kb() -> ReplyKeyboardMarkup:
    # Одна широкая кнопка
    rows = [[KeyboardButton(KB_MAIN_LABEL)]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, one_time_keyboard=False)

def _silent(text: str) -> str:
    # Для служебных сообщений можно использовать короткий текст
    return text

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
    return d.strftime("%a")  # англ. аббревиатуры

def _format_table_for_range(tasks: list[dict], start: date, end: date, title: str) -> str:
    """
    Моноширинная табличка для /today, /week, /month (в меню/командах).
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

def _format_plain_for_date(tasks: list[dict], d: date) -> str:
    """
    Обычный текст (НЕ моно) — для дневных напоминаний.
    """
    m = _tasks_by_date_map(tasks)
    day_tasks = m.get(d, [])
    if not day_tasks:
        return ""
    day_tasks_sorted = sorted(
        day_tasks,
        key=lambda t: (_extract_time_from_title(t.get('title') or "")[0] or "99:99")
    )
    lines = [f"📅 Стримы сегодня — {d.strftime('%d.%m.%Y')}:", ""]
    for t in day_tasks_sorted:
        hhmm, cleaned_title = _extract_time_from_title(t.get("title") or "")
        if hhmm:
            lines.append(f"• {hhmm} — {cleaned_title}")
        else:
            lines.append(f"• {cleaned_title}")
    lines.append("")
    lines.append("Залетайте на стримчики! 🤝")
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

def is_twitch_live() -> bool:
    """Простая проверка: сейчас есть эфир у пользователя? (без сравнения stream_id)"""
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
            timeout=20,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return bool(data)
    except Exception as e:
        print(f"[TW] is_live error: {e}")
        return False

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

# ==================== ПОСТИНГ ====================
async def tg_broadcast_photo(app: Application, chat_ids: list[str], text: str, kb: InlineKeyboardMarkup | None, photo_url: str, silent: bool = False):
    for chat_id in chat_ids:
        # 1) фото
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
            print(f"[TG] photo send failed for {chat_id}: {e}. Fallback to link+message.")
        except Exception as e:
            print(f"[TG] photo send error to {chat_id}: {e}. Fallback to link+message.")

        # 2) фолбэк
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

def build_stream_buttons(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    yt_url = (
        f"https://www.youtube.com/watch?v={youtube_video_id}"
        if youtube_video_id else
        (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    )
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ Гоу на YouTube", url=yt_url),
         InlineKeyboardButton("💜 Гоу на Twitch",  url=tw_url)],
        [InlineKeyboardButton("🤙 Гоу в клан", url="https://t.me/D13_join_bot")]
    ])

def build_hourly_buttons(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    yt_url = (
        f"https://www.youtube.com/watch?v={youtube_video_id}"
        if youtube_video_id else
        (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    )
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ YouTube", url=yt_url),
         InlineKeyboardButton("💜 Twitch",  url=tw_url)],
    ])

def build_schedule_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")]
    ])

async def _announce_with_sources(app: Application, title: str, yt_video: dict | None):
    yt_id = yt_video["id"] if yt_video else None
    photo_url = (yt_video.get("thumb") if (yt_video and yt_video.get("thumb")) else STATIC_IMAGE_URL)
    text = (
        "🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{html_escape(title or '')}</b>\n\n"
        "#DEKTRIAN #D13 #ОНЛАЙН"
    )
    kb = build_stream_buttons(yt_id)
    await tg_broadcast_photo(app, STREAM_CHAT_IDS, text, kb, photo_url, silent=False)

async def start_hourly_reminders(app: Application, yt_video_id: str | None):
    """Запускаем/перезапускаем почасовые напоминания. Проверка онлайна — только Twitch."""
    global HOURLY_TASK
    # отменим предыдущую, если была
    if HOURLY_TASK and not HOURLY_TASK.done():
        HOURLY_TASK.cancel()
        try:
            await HOURLY_TASK
        except Exception:
            pass

    async def _loop():
        try:
            # небольшой стартовый лаг, чтобы не наслаиваться с основным анонсом
            await asyncio.sleep(LIVE_REMIND_EVERY_MIN * 60)
            while True:
                if not is_twitch_live():
                    break
                # удалить старое напоминание
                for chat_id, mid in list(LAST_HOURLY_MSG_ID.items()):
                    try:
                        await app.bot.delete_message(chat_id=chat_id, message_id=mid)
                    except Exception:
                        pass
                # отправить новое
                text = "⏰ Мы всё ещё на стриме — врывайся! 😏"
                kb = build_hourly_buttons(yt_video_id)
                for chat_id in STREAM_CHAT_IDS:
                    try:
                        msg = await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, disable_notification=True)
                        LAST_HOURLY_MSG_ID[str(chat_id)] = msg.message_id
                    except Exception as e:
                        print(f"[HOURLY] send error to {chat_id}: {e}")
                # ждать следующий цикл
                await asyncio.sleep(LIVE_REMIND_EVERY_MIN * 60)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[HOURLY] loop error: {e}")

    HOURLY_TASK = asyncio.create_task(_loop())

# ==================== ОСНОВНАЯ ЛОГИКА (фон) ====================
async def minute_loop(app: Application):
    print(f"[WAKE] minute loop started at {now_local().isoformat()}")
    while True:
        try:
            if _sec_since(_last_called_ts["tw"]) >= 60:
                print("[WAKE] tick: twitch check")
                tw = twitch_check_live()
                if tw:
                    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
                    title = tw.get("title") or (yt_live.get("title") if yt_live else "Стрим")
                    await _announce_with_sources(app, title, yt_live)
                    yt_id = yt_live["id"] if yt_live else None
                    await start_hourly_reminders(app, yt_id)
                _last_called_ts["tw"] = int(time.time())
        except Exception as e:
            print(f"[WAKE] loop error: {e}")
        await asyncio.sleep(5)

async def schedule_loop(app: Application):
    """
    Каждую минуту проверяем, не пора ли отправить дневное напоминание.
    """
    sent_marks: set[str] = set()  # ключи вида "YYYY-MM-DD|HH:MM"
    print(f"[SCHEDULE] loop started at {now_local().isoformat()}")
    while True:
        try:
            now = now_local()
            key = f"{now.date().isoformat()}|{now.strftime('%H:%M')}"
            if now.strftime("%H:%M") in DAILY_REMIND_AT and key not in sent_marks:
                tasks = _tasks_fetch_all()
                today = now.date()
                plain = _format_plain_for_date(tasks, today)
                if plain:
                    # есть стримы -> шлём
                    kb = build_schedule_buttons()
                    await tg_broadcast_photo(
                        app=app,
                        chat_ids=SCHEDULE_CHAT_IDS,
                        text=plain,
                        kb=kb,
                        photo_url=SCHEDULE_IMAGE_URL,
                        silent=True,
                    )
                    sent_marks.add(key)
                else:
                    print("[SCHEDULE] no tasks today -> skip posting")
        except Exception as e:
            print(f"[SCHEDULE] loop error: {e}")
        await asyncio.sleep(30)

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

# ==================== ВСПОМОГАТЕЛЬНОЕ: якорные сообщения ====================
async def _send_anchor(app: Application, chat_id: str, text: str, kb: InlineKeyboardMarkup | None):
    """Создаём новое «якорное» сообщение (тихо), удаляя предыдущее."""
    old_id = ANCHOR_BY_CHAT.get(str(chat_id))
    if old_id:
        try:
            await app.bot.delete_message(chat_id=chat_id, message_id=old_id)
        except Exception:
            pass
    msg = await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML", disable_notification=True)
    ANCHOR_BY_CHAT[str(chat_id)] = msg.message_id

# ==================== КОМАНДЫ (Tasks) ====================
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

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    tasks = _tasks_fetch_all()
    d = now_local().date()
    text = _format_table_for_range(tasks, d, d, f"📅 Сегодня — {d.strftime('%d.%m.%Y')}")
    chat_id = str(update.effective_chat.id)
    await _send_anchor(context.application, chat_id, text, _main_menu_kb())

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    tasks = _tasks_fetch_all()
    start = now_local().date()
    end = start + timedelta(days=6)
    text = _format_table_for_range(tasks, start, end, f"🗓 Неделя — {start.strftime('%d.%m')}–{end.strftime('%d.%m')}")
    chat_id = str(update.effective_chat.id)
    await _send_anchor(context.application, chat_id, text, _main_menu_kb())

def _month_weeks(year: int, month: int) -> list[tuple[date, date]]:
    last_day = calendar.monthrange(year, month)[1]
    weeks = []
    d0 = date(year, month, 1)
    d = d0
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
        [InlineKeyboardButton("◀️", callback_data=f"m|{ym}|{prev_idx}"),
         InlineKeyboardButton(f"Неделя {idx+1}/{total}", callback_data=f"m|{ym}|{idx}"),
         InlineKeyboardButton("▶️", callback_data=f"m|{ym}|{next_idx}")],
        [InlineKeyboardButton("↩︎ В меню", callback_data="menu|main")]
    ])

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    today = now_local().date()
    year, month = today.year, today.month
    weeks = _month_weeks(year, month)
    idx = 0
    tasks = _tasks_fetch_all()
    start, end = weeks[idx]
    text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
    kb = _month_kb(f"{year:04d}-{month:02d}", idx, len(weeks))
    chat_id = str(update.effective_chat.id)
    await _send_anchor(context.application, chat_id, text, kb)

# ==================== КОМАНДЫ: меню и кнопки ====================
def _main_menu_kb() -> InlineKeyboardMarkup:
    # Два столбика
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
         InlineKeyboardButton("🗓 Неделя", callback_data="t|week")],
        [InlineKeyboardButton("📆 Месяц", callback_data="t|month"),
         InlineKeyboardButton("Соцсети стримера", callback_data="menu|socials")],
        [InlineKeyboardButton("Бронь стрима", url="https://t.me/DektrianTV"),
         InlineKeyboardButton("Купить юси", url="https://t.me/uc_pubg_bounty")],
        [InlineKeyboardButton("Вступить в клан", url="https://t.me/D13_join_bot")],
    ])

def _socials_kb() -> InlineKeyboardMarkup:
    yt = SOC_YT or (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    tw = SOC_TWITCH or (f"https://www.twitch.tv/{TWITCH_USERNAME}" if TWITCH_USERNAME else "https://www.twitch.tv/")
    rows = [
        [InlineKeyboardButton("YouTube", url=yt),
         InlineKeyboardButton("Twitch", url=tw)],
        [InlineKeyboardButton("Группа Telegram", url=SOC_TG_GROUP),
         InlineKeyboardButton("Канал Telegram", url=SOC_TG_CHANNEL)],
    ]
    if SOC_TIKTOK:
        rows.append([InlineKeyboardButton("TikTok", url=SOC_TIKTOK),
                     InlineKeyboardButton("↩︎ Назад", callback_data="menu|main")])
    else:
        rows.append([InlineKeyboardButton("↩︎ Назад", callback_data="menu|main")])
    return InlineKeyboardMarkup(rows)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    await _send_anchor(context.application, chat_id, "Меню бота:", _main_menu_kb())

async def on_menu_nav(query_data: str, query, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(query.message.chat_id)
    if query_data == "menu|socials":
        await _send_anchor(context.application, chat_id, "Соцсети стримера:", _socials_kb())
    elif query_data == "menu|main":
        await _send_anchor(context.application, chat_id, "Меню бота:", _main_menu_kb())

# ==================== КОМАНДЫ: тест анонса (симуляция старта) ====================
async def cmd_test1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Симулируем старт: анонс + запуск почасовых напоминаний."""
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await _announce_with_sources(context.application, title, yt_live)
    yt_id = yt_live["id"] if yt_live else None
    await start_hourly_reminders(context.application, yt_id)
    if update.effective_message:
        await update.effective_message.reply_text("ОК: сымитировал старт стрима (анонс отправлен, напоминалки запущены).")

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
async def _install_keyboard_silently(app: Application, chat_id: str):
    """Отправляем «служебное» сообщение с Reply-клавой и сразу удаляем его (без звука)."""
    try:
        msg = await app.bot.send_message(chat_id=chat_id, text=_silent("…"), reply_markup=main_reply_kb(), disable_notification=True)
        # небольшая пауза — чтобы клиенты успели применить клавиатуру
        await asyncio.sleep(0.3)
        try:
            await app.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass
    except Exception as e:
        print(f"[STARTUP] keyboard setup failed in {chat_id}: {e}")

async def _on_start(app: Application):
    # 1) Публичные команды (test1 не показываем)
    await app.bot.set_my_commands([
        BotCommand("today", "📅 Стримы сегодня"),
        BotCommand("week", "🗓 Стримы на неделю"),
        BotCommand("month", "📆 Стримы за месяц (по неделям)"),
        BotCommand("menu", "Открыть меню"),
    ])

    # 2) Тихая доставка Reply-клавиатуры в целевые чаты (и мгновенное удаление служебки)
    target_chats = set(CHAT_IDS) | set(STREAM_CHAT_IDS) | set(SCHEDULE_CHAT_IDS)
    for chat_id in target_chats:
        await _install_keyboard_silently(app, chat_id)

    # 3) Фоновые задачи
    asyncio.create_task(minute_loop(app))
    asyncio.create_task(schedule_loop(app))
    asyncio.create_task(self_ping())
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")

# ==================== ROUTING ====================
async def on_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем только точное нажатие нашей широкой кнопки Reply-клавиатуры."""
    if not update.effective_message or not update.effective_message.text:
        return
    text = update.effective_message.text.strip()
    if text == KB_MAIN_LABEL:
        await cmd_menu(update, context)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    if data.startswith("m|"):         # month weeks nav
        try:
            _, ym, idx_str = data.split("|")
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
        await _send_anchor(context.application, str(q.message.chat_id), text, kb)

    elif data.startswith("menu|"):
        await on_menu_nav(data, q, context)

    elif data.startswith("t|"):
        action = data.split("|", 1)[1]
        dummy_update = Update(update.update_id, message=q.message)  # для reuse chat_id
        if action == "today":
            await cmd_today(dummy_update, context)
        elif action == "week":
            await cmd_week(dummy_update, context)
        elif action == "month":
            await cmd_month(dummy_update, context)

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
    application.add_handler(CommandHandler("test1", cmd_test1))  # скрытая команда, не в set_my_commands
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week", cmd_week))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("menu", cmd_menu))

    # Текстовые кнопки (ReplyKeyboard) — только точное совпадение
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
