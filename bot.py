# bot.py
import os
import time
import asyncio
import re
import calendar
from datetime import datetime, timedelta, timezone, date

import requests
import aiohttp  # self-ping для бесплатного хоста
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

# Публичный URL для вебхука (Render и т.п.)
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "8080")))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dektrian-secret")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", f"/telegram/{BOT_NAME}")

# Киев: летом UTC+3, зимой UTC+2 — вручную
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# === Google Tasks (для /today /week /month и напоминалок) ===
GOOGLE_TASKS_CLIENT_ID = os.getenv("GOOGLE_TASKS_CLIENT_ID", "").strip()
GOOGLE_TASKS_CLIENT_SECRET = os.getenv("GOOGLE_TASKS_CLIENT_SECRET", "").strip()
GOOGLE_TASKS_REFRESH_TOKEN = os.getenv("GOOGLE_TASKS_REFRESH_TOKEN", "").strip()
GOOGLE_TASKS_LIST_ID = os.getenv("GOOGLE_TASKS_LIST_ID", "").strip()

# YouTube (используется для превью при старте стрима + кнопки)
YT_API_KEY = os.getenv("YT_API_KEY", "").strip()
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "").strip()

# Twitch (детект старта стрима)
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME", "dektrian_tv").strip()

# Картинка по умолчанию для поста о старте стрима
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()

# Картинка для ежедневных напоминаний о расписании (ЛЕГКО сменить ссылку тут)
SCHEDULE_IMAGE_URL = os.getenv("SCHEDULE_IMAGE_URL", "https://ibb.co/LXSMV1FQ").strip()

# ===== Чаты для постинга (ЛЕГКО редактировать здесь) =====
# Сюда летят посты о старте стрима + «мы всё ещё в эфире»
STREAM_CHAT_IDS = [
    # Примеры: "-1001234567890"
]
# Сюда летят ЕЖЕДНЕВНЫЕ напоминания о стримах на сегодня
SCHEDULE_CHAT_IDS = [
    # Примеры: "-1009876543210"
]
# Если списки выше пустые, используем TELEGRAM_CHAT_IDS / TELEGRAM_CHANNEL_ID из ENV (обратная совместимость)
_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
if not STREAM_CHAT_IDS:
    STREAM_CHAT_IDS = [c.strip() for c in _raw_chats.split(",") if c.strip()]
if not SCHEDULE_CHAT_IDS:
    SCHEDULE_CHAT_IDS = [c.strip() for c in _raw_chats.split(",") if c.strip()]

# Ежедневные напоминания (локальное время Киева), НЕ зависят от ENV — меняются тут
SCHEDULE_POST_TIMES = ["10:00", "14:00"]  # список "HH:MM"

# Параметры периодических «пинков» во время эфира
LIVE_PING_EVERY_MINUTES = 60   # раз в час
LIVE_PING_MAX_HOURS = 6        # не более 6 часов напоминаний

# ========= In-memory state =========
last_twitch_stream_id: str | None = None
_tw_token: str | None = None
_tw_token_expire_at: int = 0  # unix ts
_last_called_ts = {"tw": 0}

# Таск периодических «мы всё ещё в эфире»
_live_ping_task: asyncio.Task | None = None

# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )

# ==================== КНОПКИ/МЕНЮ ====================
# Текстовые кнопки (ReplyKeyboard) — ровно по подписям, чтобы не триггериться на обычные слова
BTN_TODAY  = "📺 Стрим сегодня"
BTN_WEEK   = "📺 Стримы неделя"
BTN_MONTH  = "📺 Стримы месяц"
BTN_MENU   = "☰ Меню"

def main_reply_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_TODAY), KeyboardButton(BTN_WEEK)],
        [KeyboardButton(BTN_MONTH), KeyboardButton(BTN_MENU)],
    ]
    # 2 столбца, закреплённая клавиатура
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, one_time_keyboard=False)

def _short_stream_kb(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    """Две кнопки: YouTube и Twitch для «мы ещё в эфире»."""
    yt_url = (
        f"https://www.youtube.com/watch?v={youtube_video_id}"
        if youtube_video_id else
        (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    )
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("❤️ YouTube", url=yt_url),
                                  InlineKeyboardButton("💜 Twitch",  url=tw_url)]])

def _start_stream_kb(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    """Полный набор для поста о старте."""
    yt_url = (
        f"https://www.youtube.com/watch?v={youtube_video_id}"
        if youtube_video_id else
        (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    )
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ Гоу на YouTube", url=yt_url),
         InlineKeyboardButton("💜 Гоу на Twitch",  url=tw_url)],
        [InlineKeyboardButton("💸 Гоу Донатик", url="https://new.donatepay.ru/@Dektrian_TV"),
         InlineKeyboardButton("🤙 Гоу в клан",  url="https://t.me/D13_join_bot")]
    ])

def _schedule_only_clan_kb() -> InlineKeyboardMarkup:
    """Для ЕЖЕДНЕВНЫХ напоминаний — только одна кнопка «Вступить в клан»."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")]])

def _main_menu_kb() -> InlineKeyboardMarkup:
    """Инлайн-меню в ДВА столбца."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
         InlineKeyboardButton("🗓 Неделя",  callback_data="t|week")],
        [InlineKeyboardButton("📆 Месяц",   callback_data="t|month")],
        [InlineKeyboardButton("Бронь стрима", url="https://t.me/DektrianTV"),
         InlineKeyboardButton("Купить юси",    url="https://t.me/uc_pubg_bounty")],
        [InlineKeyboardButton("Вступить в клан", url="https://t.me/D13_join_bot"),
         InlineKeyboardButton("Соцсети стримера", callback_data="menu|socials")],
    ])

def _socials_kb() -> InlineKeyboardMarkup:
    """Соцсети в ДВА столбца + «Назад»."""
    rows = [
        [InlineKeyboardButton("YouTube", url="https://www.youtube.com/@Dektrian_TV"),
         InlineKeyboardButton("Twitch",  url="https://www.twitch.tv/dektrian_tv")],
        [InlineKeyboardButton("Группа Telegram", url="https://t.me/dektrian_tv"),
         InlineKeyboardButton("Канал Telegram",  url="https://t.me/dektrian_family")],
        [InlineKeyboardButton("TikTok", url="https://www.tiktok.com/@dektrian_tv"),
         InlineKeyboardButton("← Назад", callback_data="menu|main")],
    ]
    return InlineKeyboardMarkup(rows)

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

def _truncate(s: str, max_len: int) -> str:
    s = s.strip()
    return (s if len(s) <= max_len else (s[: max_len - 1] + "…"))

def _format_table_for_range(tasks: list[dict], start: date, end: date, title: str, max_title_len: int = 36) -> str:
    """
    Моноширинная «таблица». Для /today используем меньший max_title_len, чтобы не было переноса.
    Колонки: Дата | Дн | Время | Событие. Пустые даты → "--" и "нет стримов".
    """
    m = _tasks_by_date_map(tasks)
    lines = []
    lines.append(html_escape(title))
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
            title_str = _truncate(html_escape(cleaned_title), max_title_len)
            if first:
                lines.append(f"{day:8} {wd:3} {time_str:5}  {title_str}")
                first = False
            else:
                lines.append(f"{'':8} {'':3} {time_str:5}  {title_str}")
    lines.append("</pre>")
    return "\n".join(lines)

def _format_plain_for_day(tasks: list[dict], day: date) -> str:
    """Простой (НЕ моно) список для ежедневных напоминаний."""
    header = f"📅 Стримы сегодня — {day.strftime('%d.%m.%Y')}"
    if not tasks:
        return header + "\n\nСегодня стримов нет."
    # сортировка
    tasks_sorted = sorted(
        tasks,
        key=lambda t: (_extract_time_from_title(t.get('title') or "")[0] or "99:99")
    )
    lines = [header, ""]
    for t in tasks_sorted:
        hhmm, title = _extract_time_from_title(t.get("title") or "")
        if hhmm:
            lines.append(f"• {hhmm} — {html_escape(title)}")
        else:
            lines.append(f"• {html_escape(title)}")
    lines.append("\nЗалетай на стримчики! 😉")
    return "\n".join(lines)

# ==================== ОТПРАВКА ====================
async def _send_photo_or_fallback(app: Application, chat_id: str, text: str, kb: InlineKeyboardMarkup | None, photo_url: str):
    """Сначала как фото-баннер, если не удалось — как текст со ссылкой (превью включено)."""
    try:
        await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode="HTML", reply_markup=kb)
        return
    except BadRequest as e:
        print(f"[TG] photo send failed for {chat_id}: {e}. Fallback to link+message.")
    except Exception as e:
        print(f"[TG] photo send error to {chat_id}: {e}. Fallback to link+message.")
    try:
        await app.bot.send_message(chat_id=chat_id, text=f"{photo_url}\n\n{text}", parse_mode="HTML",
                                   reply_markup=kb, disable_web_page_preview=False)
    except Exception as e:
        print(f"[TG] message send error to {chat_id}: {e}")

async def post_start_stream(app: Application, title: str, youtube_video_id: str | None, photo_url: str | None):
    """Пост в STREAM_CHAT_IDS о старте стрима."""
    text = (
        "🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{html_escape(title or 'Стрим')}</b>\n\n"
        "#DEKTRIAN #D13 #ОНЛАЙН"
    )
    kb = _start_stream_kb(youtube_video_id)
    for chat_id in STREAM_CHAT_IDS:
        await _send_photo_or_fallback(app, chat_id, text, kb, photo_url or STATIC_IMAGE_URL)

async def post_live_ping(app: Application, youtube_video_id: str | None):
    """Короткий пост «мы всё ещё в эфире» в STREAM_CHAT_IDS."""
    text = "Мы всё ещё на стриме, врывайся! 😏"
    kb = _short_stream_kb(youtube_video_id)
    for chat_id in STREAM_CHAT_IDS:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            print(f"[TG] live ping send error to {chat_id}: {e}")

async def post_schedule_reminder(app: Application, text: str, image_url: str | None):
    """Ежедневные напоминания (только кнопка «вступить в клан», без моно)."""
    kb = _schedule_only_clan_kb()
    for chat_id in SCHEDULE_CHAT_IDS:
        await _send_photo_or_fallback(app, chat_id, text, kb, image_url or SCHEDULE_IMAGE_URL)

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
    """Возвращает {'id': stream_id, 'title': title} если эфир ЛАЙВ, иначе None.
       ВАЖНО: мы считаем «новым стартом» только изменение stream_id."""
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
        # Если новый stream_id — считаем это свежим стартом
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
            # сброс токена и одна повторная попытка
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

# ==================== ЛОГИКА СТАРТА + ПИНГОВ ЛАЙВА ====================
async def _announce_with_sources(app: Application, forced_title: str | None = None):
    """Формирование поста о старте: пытаемся взять превью с YouTube, иначе — статика."""
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = forced_title or (yt_live.get("title") if yt_live else "Стрим")
    yt_id = yt_live["id"] if yt_live else None
    photo_url = (yt_live.get("thumb") if (yt_live and yt_live.get("thumb")) else STATIC_IMAGE_URL)
    await post_start_stream(app, title, yt_id, photo_url)
    return yt_id  # пригодится для «мы всё ещё в эфире»

async def _live_ping_loop(app: Application, initial_yt_id: str | None):
    """Периодические «мы всё ещё в эфире» — раз в час до конца эфира/лимита."""
    try:
        yt_id = initial_yt_id
        for _ in range(LIVE_PING_MAX_HOURS):
            await asyncio.sleep(LIVE_PING_EVERY_MINUTES * 60)
            # Проверим, эфир ещё идёт?
            live_now = twitch_check_live()
            if not live_now:
                break
            # Раз в час можно попробовать уточнить YouTube id (может появиться позже)
            if not yt_id:
                res = await yt_fetch_live_with_retries(max_attempts=1, delay_seconds=1)
                yt_id = (res or {}).get("id")
            await post_live_ping(app, yt_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[LIVE PING] loop error: {e}")

async def minute_loop(app: Application):
    """Каждую минуту проверяем Twitch. Если старт — постим и запускаем пинги."""
    global _live_ping_task
    print(f"[WAKE] minute loop started at {now_local().isoformat()}")
    while True:
        try:
            if _sec_since(_last_called_ts["tw"]) >= 60:
                print("[WAKE] tick: twitch check")
                tw = twitch_check_live()
                if tw:
                    yt_id = await _announce_with_sources(app, forced_title=tw.get("title") or None)
                    # стартуем пинги, если ещё не идут
                    if _live_ping_task is None or _live_ping_task.done():
                        _live_ping_task = asyncio.create_task(_live_ping_loop(app, yt_id))
                _last_called_ts["tw"] = int(time.time())
        except Exception as e:
            print(f"[WAKE] loop error: {e}")
        await asyncio.sleep(5)

# ==================== ЕЖЕДНЕВНЫЕ НАПОМИНАНИЯ ====================
_daily_posted: dict[str, set[str]] = {}  # key=date_str 'YYYY-MM-DD' -> set{"HH:MM"}

def _should_post_now(now: datetime, hhmm: str) -> bool:
    return now.strftime("%H:%M") == hhmm

async def _daily_reminder_tick(app: Application):
    """Проверяем каждые ~20 сек, наступило ли одно из заданных SCHEDULE_POST_TIMES.
       Постим ТОЛЬКО если на сегодня есть задачи в Google Tasks."""
    print("[REMINDER] daily reminder tick started")
    while True:
        try:
            now = now_local()
            day_key = now.strftime("%Y-%m-%d")
            posted_set = _daily_posted.setdefault(day_key, set())

            # Сброс трекера по новому дню
            for k in list(_daily_posted.keys()):
                if k != day_key:
                    _daily_posted.pop(k, None)

            for hhmm in SCHEDULE_POST_TIMES:
                if hhmm in posted_set:
                    continue
                if _should_post_now(now, hhmm):
                    # Собираем задачи на сегодня
                    tasks = _tasks_fetch_all()
                    today = now.date()
                    todays = [t for t in tasks if _due_to_local_date(t.get("due") or "") == today]
                    if todays:
                        text = _format_plain_for_day(todays, today)  # НЕ моно, без лишних кнопок
                        await post_schedule_reminder(app, text, SCHEDULE_IMAGE_URL)
                        posted_set.add(hhmm)
                    else:
                        print(f"[REMINDER] {hhmm}: пропускаем — на сегодня нет задач")
        except Exception as e:
            print(f"[REMINDER] tick error: {e}")
        await asyncio.sleep(20)

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
    # Для СЕГОДНЯ — жёстче урежем длину, чтобы не было переноса
    text = _format_table_for_range(tasks, d, d, f"📅 Сегодня — {d.strftime('%d.%m.%Y')}", max_title_len=28)
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=main_reply_kb())

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    tasks = _tasks_fetch_all()
    start = now_local().date()
    end = start + timedelta(days=6)
    text = _format_table_for_range(tasks, start, end, f"🗓 Неделя — {start.strftime('%d.%m')}–{end.strftime('%d.%m')}")
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=main_reply_kb())

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
        [InlineKeyboardButton("◀️", callback_data=f"m|{ym}|{prev_idx}"),
         InlineKeyboardButton(f"Неделя {idx+1}/{total}", callback_data=f"m|{ym}|{idx}"),
         InlineKeyboardButton("▶️", callback_data=f"m|{ym}|{next_idx}")],
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
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)

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

# ==================== МЕНЮ/НАВИГАЦИЯ ====================
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text("Меню бота:", reply_markup=_main_menu_kb())

async def on_menu_nav(query_data: str, query, context: ContextTypes.DEFAULT_TYPE):
    if query_data == "menu|socials":
        await query.edit_message_text("Соцсети стримера:", reply_markup=_socials_kb())
    elif query_data == "menu|main":
        await query.edit_message_text("Меню бота:", reply_markup=_main_menu_kb())

# ==================== КОМАНДА TEST1 ====================
async def cmd_test1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Имитирует старт стрима:
       - берём превью с YouTube (3 попытки), иначе статичная картинка
       - постим «стрим начался» в STREAM_CHAT_IDS
       - запускаем часовые «мы ещё в эфире» (как при настоящем старте)"""
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    yt_id = (yt_live or {}).get("id")
    title = (yt_live or {}).get("title") or f"Тестовый пост от {BOT_NAME}"
    photo = (yt_live or {}).get("thumb") or STATIC_IMAGE_URL
    await post_start_stream(context.application, title, yt_id, photo)

    # Поднимем имитацию пингов
    global _live_ping_task
    if _live_ping_task is None or _live_ping_task.done():
        _live_ping_task = asyncio.create_task(_live_ping_loop(context.application, yt_id))

    if update.effective_message:
        await update.effective_message.reply_text("Тест1: отправил анонс и запустил часовые пинги.", reply_markup=main_reply_kb())

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
    # 1) Видимые slash-команды (латиница). /test1 намеренно НЕ публикуем.
    await app.bot.set_my_commands([
        BotCommand("today", "📅 Стримы сегодня"),
        BotCommand("week",  "🗓 Стримы на неделю"),
        BotCommand("month", "📆 Стримы за месяц (по неделям)"),
        BotCommand("menu",  "Открыть меню"),
    ])

    # 2) Показать закреплённую клавиатуру в целевых чатах (по твоей просьбе — оставляем)
    all_chats = set(STREAM_CHAT_IDS) | set(SCHEDULE_CHAT_IDS)
    for chat_id in all_chats:
        try:
            await app.bot.send_message(chat_id=chat_id, text="Меню бота:", reply_markup=main_reply_kb())
        except Exception as e:
            print(f"[STARTED] cannot show keyboard in {chat_id}: {e}")

    # 3) Фоновые задачи
    asyncio.create_task(minute_loop(app))          # детект старта стрима
    asyncio.create_task(self_ping())               # поддержка живости хоста
    asyncio.create_task(_daily_reminder_tick(app)) # ежедневные напоминания

    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")

# ==================== ROUTING ====================
# Обработка «ReplyKeyboard» — реагируем ТОЛЬКО на точное совпадение с нашими кнопками.
async def on_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_message.text:
        return
    text = update.effective_message.text.strip()
    if text == BTN_TODAY:
        await cmd_today(update, context)
    elif text == BTN_WEEK:
        await cmd_week(update, context)
    elif text == BTN_MONTH:
        await cmd_month(update, context)
    elif text == BTN_MENU:
        await cmd_menu(update, context)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()  # ack
    if data.startswith("m|"):         # навигация по неделям месяца
        await on_month_nav(data, q, context)
    elif data.startswith("menu|"):    # инлайн меню
        await on_menu_nav(data, q, context)
    elif data.startswith("t|"):       # быстрые кнопки t|today|week|month
        action = data.split("|", 1)[1]
        dummy_update = Update(update.update_id, message=q.message)  # переиспользуем message
        if action == "today":
            await cmd_today(dummy_update, context)
        elif action == "week":
            await cmd_week(dummy_update, context)
        elif action == "month":
            await cmd_month(dummy_update, context)

# Self-ping для бесплатного хоста
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

def main():
    if not TG_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in Environment")
    if not PUBLIC_URL:
        raise SystemExit("Set PUBLIC_URL (https://<your-host>) for webhook (или используйте RENDER_EXTERNAL_URL)")

    application = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(_on_start)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("test1", cmd_test1))   # скрытая команда для тебя
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week",  cmd_week))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("menu",  cmd_menu))

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
