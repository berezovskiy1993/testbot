import os
import time
import asyncio
import re
import calendar
from datetime import datetime, timedelta, timezone, date

import requests
import aiohttp
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

# ========= ENV & SIMPLE CONFIG =========
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Куда постим анонсы про «стрим начался» и почасовые «мы ещё в эфире»
_raw_stream_chats = (os.getenv("TELEGRAM_BOT_IDS") or os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
STREAM_CHAT_IDS = [c.strip() for c in _raw_stream_chats.split(",") if c.strip()]

# Куда постим ЕЖЕДНЕВНЫЕ напоминалки расписания
_raw_reminder_chats = (os.getenv("REMINDER_CHAT_IDS") or "").strip()
REMINDER_CHAT_IDS = [c.strip() for c in _raw_reminder_chats.split(",") if c.strip()] or STREAM_CHAT_IDS

# Киев: летом UTC+3, зимой UTC+2 — вручную
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# === Google Tasks (для расписания) ===
GOOGLE_TASKS_CLIENT_ID = os.getenv("GOOGLE_TASKS_CLIENT_ID", "").strip()
GOOGLE_TASKS_CLIENT_SECRET = os.getenv("GOOGLE_TASKS_CLIENT_SECRET", "").strip()
GOOGLE_TASKS_REFRESH_TOKEN = os.getenv("GOOGLE_TASKS_REFRESH_TOKEN", "").strip()
GOOGLE_TASKS_LIST_ID = os.getenv("GOOGLE_TASKS_LIST_ID", "").strip()

# YouTube
YT_API_KEY = os.getenv("YT_API_KEY", "").strip()
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "").strip()

# Twitch
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME", "dektrian_tv").strip()

# Картинки
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()  # баннер для анонса
SCHEDULE_REMINDER_IMAGE_URL = os.getenv("SCHEDULE_REMINDER_IMAGE_URL", "https://ibb.co/LXSMV1FQ").strip()  # картинка для ежедневных напоминаний

# Соцсети (опционально)
SOC_YT = os.getenv("SOCIAL_YOUTUBE", "").strip()
SOC_TWITCH = os.getenv("SOCIAL_TWITCH", "").strip()
SOC_TG = os.getenv("SOCIAL_TELEGRAM", "https://t.me/DektrianTV").strip()
SOC_TIKTOK = os.getenv("SOCIAL_TIKTOK", "").strip()
SOC_IG = os.getenv("SOCIAL_INSTAGRAM", "").strip()
SOC_X = os.getenv("SOCIAL_X", "").strip()
SOC_DISCORD = os.getenv("SOCIAL_DISCORD", "").strip()

# Вебхук
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "8080")))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dektrian-secret")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", f"/telegram/{BOT_NAME}")

# Напоминалки: локальное время HH:MM (меняешь прямо в коде)
SCHEDULE_REMINDER_TIMES = ["10:00", "14:00"]

# Почасовой пинг «мы всё ещё в эфире»
HOURLY_PING_ENABLED = True
HOURLY_PING_INTERVAL_MIN = 60

# ========= In-memory state =========
last_twitch_stream_id: str | None = None
current_live_stream_id: str | None = None  # активный эфир (для почасовых пингов)
hourly_ping_task: asyncio.Task | None = None

# карта «чат -> id панельного сообщения», которое мы редактируем
PANEL_MSG_IDS: dict[int, int] = {}

_tw_token: str | None = None
_tw_token_expire_at: int = 0
_last_called_ts = {"tw_check": 0, "reminders": 0}

# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ==================== TELEGRAM UI ====================
def build_main_reply_kb() -> ReplyKeyboardMarkup:
    # используем эмодзи, чтобы не срабатывало от обычных слов
    rows = [
        [KeyboardButton("📺 Стрим сегодня"), KeyboardButton("📺 Стримы неделя")],
        [KeyboardButton("📺 Стримы месяц"), KeyboardButton("☰ Меню")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, one_time_keyboard=False)

def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
         InlineKeyboardButton("🗓 Неделя", callback_data="t|week")],
        [InlineKeyboardButton("📆 Месяц", callback_data="t|month")],
        [InlineKeyboardButton("Бронь стрима", url="https://t.me/DektrianTV")],
        [InlineKeyboardButton("Купить юси", url="https://t.me/uc_pubg_bounty")],
        [InlineKeyboardButton("Вступить в клан", url="https://t.me/D13_join_bot")],
        [InlineKeyboardButton("Соцсети стримера", callback_data="menu|socials")],
    ])

def _socials_kb() -> InlineKeyboardMarkup:
    yt = SOC_YT or (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    tw = SOC_TWITCH or (f"https://www.twitch.tv/{TWITCH_USERNAME}" if TWITCH_USERNAME else "https://www.twitch.tv/")
    tg = SOC_TG or "https://t.me/DektrianTV"
    rows = [
        [InlineKeyboardButton("YouTube", url=yt),
         InlineKeyboardButton("Twitch", url=tw)],
        [InlineKeyboardButton("Группа Telegram", url="https://t.me/dektrian_tv"),
         InlineKeyboardButton("Канал Telegram", url="https://t.me/dektrian_family")],
        [InlineKeyboardButton("TikTok", url=SOC_TIKTOK or "https://www.tiktok.com/@dektrian_tv")],
        [InlineKeyboardButton("← Назад", callback_data="menu|main")],
    ]
    return InlineKeyboardMarkup(rows)

def build_watch_kb(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    yt_url = (f"https://www.youtube.com/watch?v={youtube_video_id}"
              if youtube_video_id else
              (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv"))
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("❤️ YouTube", url=yt_url),
                                  InlineKeyboardButton("💜 Twitch", url=tw_url)]])

def build_full_keyboard(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    yt_url = (f"https://www.youtube.com/watch?v={youtube_video_id}"
              if youtube_video_id else
              (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv"))
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ Гоу на YouTube", url=yt_url),
         InlineKeyboardButton("💜 Гоу на Twitch",  url=tw_url)],
        [InlineKeyboardButton("💸 Гоу Донатик", url="https://new.donatepay.ru/@Dektrian_TV"),
         InlineKeyboardButton("🤙 Гоу в клан", url="https://t.me/D13_join_bot")]
    ])

# Панель: единое сообщение, которое редактируем
async def _ensure_panel_message(app: Application, chat_id: int) -> int:
    msg_id = PANEL_MSG_IDS.get(chat_id)
    if msg_id:
        return msg_id
    # создаём новую панель
    m = await app.bot.send_message(chat_id=chat_id, text="Меню бота:", reply_markup=_main_menu_kb())
    PANEL_MSG_IDS[chat_id] = m.message_id
    return m.message_id

async def _panel_set(app: Application, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None):
    msg_id = PANEL_MSG_IDS.get(chat_id)
    if not msg_id:
        msg_id = await _ensure_panel_message(app, chat_id)
    try:
        await app.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        # панель могли удалить — создаём заново
        if "message to edit not found" in str(e).lower() or "message can't be edited" in str(e).lower():
            m = await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
            PANEL_MSG_IDS[chat_id] = m.message_id
        else:
            raise

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
    Моноширинное табличное отображение.
    Пустые дни: "--" и "нет стримов".
    Без сокращения слов в названиях.
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

# ====== Генерация экранов панели ======
def _make_today_text(tasks: list[dict]) -> str:
    d = now_local().date()
    return _format_table_for_range(tasks, d, d, f"📅 Сегодня — {d.strftime('%d.%m.%Y')}")

def _make_week_text(tasks: list[dict]) -> str:
    start = now_local().date()
    end = start + timedelta(days=6)
    return _format_table_for_range(tasks, start, end, f"🗓 Неделя — {start.strftime('%d.%m')}–{end.strftime('%d.%m')}")

def _month_weeks(year: int, month: int) -> list[tuple[date, date]]:
    last_day = calendar.monthrange(year, month)[1]
    weeks = []
    d0 = date(year, month, 1)
    while d0.month == month:
        start = d0
        end = min(date(year, month, last_day), start + timedelta(days=6))
        weeks.append((start, end))
        d0 = end + timedelta(days=1)
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

def twitch_check_new_live() -> dict | None:
    """
    Возвращает {'id': stream_id, 'title': title} ТОЛЬКО при появлении НОВОГО эфира.
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
            global _tw_token, _tw_token_expire_at
            _tw_token = None
            _tw_token_expire_at = 0
            try:
                res = _call()
                if res:
                    last_twitch_stream_id = res["id"]
                return res
            except Exception as e2:
                print(f"[TW] retry failed: {e2}")
        else:
            print(f"[TW] streams HTTP {getattr(e.response,'status_code','?')}: {getattr(e.response,'text','')}")
    except Exception as e:
        print(f"[TW] error: {e}")
    return None

def twitch_is_live() -> bool:
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
    except Exception:
        return False

# ==================== ПОСТИНГ ====================
async def _announce_with_sources(app: Application, title: str, yt_video: dict | None):
    yt_id = yt_video["id"] if yt_video else None
    photo_url = (yt_video.get("thumb") if (yt_video and yt_video.get("thumb")) else STATIC_IMAGE_URL)
    text = (
        "🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{html_escape(title or '')}</b>\n\n"
        "#DEKTRIAN #D13 #ОНЛАЙН"
    )
    kb = build_full_keyboard(yt_id)
    for chat_id in STREAM_CHAT_IDS:
        try:
            await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode="HTML", reply_markup=kb)
        except BadRequest:
            # фолбэк ссылкой
            await app.bot.send_message(chat_id=chat_id, text=f"{photo_url}\n\n{text}", parse_mode="HTML", reply_markup=kb, disable_web_page_preview=False)

async def _hourly_live_pinger(app: Application):
    """
    Пока эфир жив — раз в HOURLY_PING_INTERVAL_MIN постим «мы ещё на стриме».
    """
    global current_live_stream_id
    while current_live_stream_id:
        # подстраховка — если эфир уже упал, выходим
        if not twitch_is_live():
            current_live_stream_id = None
            break

        # пост
        yt_live = await yt_fetch_live_with_retries(max_attempts=1, delay_seconds=0)
        kb = build_watch_kb(yt_live["id"] if yt_live else None)
        text = "🟢 Мы всё ещё на стриме, врывайся! 😏"
        for chat_id in STREAM_CHAT_IDS:
            try:
                await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            except Exception as e:
                print(f"[PING] send error to {chat_id}: {e}")

        # ждём час
        for _ in range(HOURLY_PING_INTERVAL_MIN):
            await asyncio.sleep(60)
            if not twitch_is_live():
                current_live_stream_id = None
                break

# ==================== КОМАНДЫ (Tasks) ====================
async def _ensure_tasks_env(update: Update) -> bool:
    ok = all([GOOGLE_TASKS_CLIENT_ID, GOOGLE_TASKS_CLIENT_SECRET, GOOGLE_TASKS_REFRESH_TOKEN, GOOGLE_TASKS_LIST_ID])
    if not ok and update.effective_message:
        await update.effective_message.reply_text(
            "❗ Не настроен доступ к Google Tasks. Нужны GOOGLE_TASKS_CLIENT_ID / SECRET / REFRESH_TOKEN / LIST_ID в ENV.",
            reply_markup=build_main_reply_kb(),
        )
    return ok

async def _show_today_panel(app: Application, chat_id: int):
    tasks = _tasks_fetch_all()
    text = _make_today_text(tasks)
    await _panel_set(app, chat_id, text, reply_markup=None)

async def _show_week_panel(app: Application, chat_id: int):
    tasks = _tasks_fetch_all()
    text = _make_week_text(tasks)
    await _panel_set(app, chat_id, text, reply_markup=None)

async def _show_month_panel(app: Application, chat_id: int):
    tasks = _tasks_fetch_all()
    today = now_local().date()
    year, month = today.year, today.month
    weeks = _month_weeks(year, month)
    idx = 0
    start, end = weeks[idx]
    text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
    kb = _month_kb(f"{year:04d}-{month:02d}", idx, len(weeks))
    await _panel_set(app, chat_id, text, reply_markup=kb)

# Команды с / — оставляем, но тоже редактируют панель
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    chat_id = update.effective_chat.id
    await _show_today_panel(context.application, chat_id)

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    chat_id = update.effective_chat.id
    await _show_week_panel(context.application, chat_id)

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    chat_id = update.effective_chat.id
    await _show_month_panel(context.application, chat_id)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await _panel_set(context.application, chat_id, "Меню бота:", _main_menu_kb())

# ==================== CALLBACKS ====================
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

async def on_menu_nav(query_data: str, query, context: ContextTypes.DEFAULT_TYPE):
    if query_data == "menu|socials":
        await query.edit_message_text("Соцсети стримера:", reply_markup=_socials_kb())
    elif query_data == "menu|main":
        await query.edit_message_text("Меню бота:", reply_markup=_main_menu_kb())

async def on_trigger_nav(query_data: str, query, context: ContextTypes.DEFAULT_TYPE):
    # t|today|week|month — редактируем ПАНЕЛЬ этого чата
    chat_id = query.message.chat_id
    if query_data == "t|today":
        await _show_today_panel(context.application, chat_id)
    elif query_data == "t|week":
        await _show_week_panel(context.application, chat_id)
    elif query_data == "t|month":
        await _show_month_panel(context.application, chat_id)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    if data.startswith("m|"):
        await on_month_nav(data, q, context)
    elif data.startswith("menu|"):
        await on_menu_nav(data, q, context)
    elif data.startswith("t|"):
        await on_trigger_nav(data, q, context)

# ==================== TEXT BUTTONS (ReplyKeyboard) ====================
TEXT_BTN_TODAY = "📺 Стрим сегодня"
TEXT_BTN_WEEK = "📺 Стримы неделя"
TEXT_BTN_MONTH = "📺 Стримы месяц"
TEXT_BTN_MENU = "☰ Меню"

async def on_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_message.text:
        return
    text = update.effective_message.text.strip()
    chat_id = update.effective_chat.id

    # реагируем ТОЛЬКО на точные надписи из клавиатуры
    if text not in (TEXT_BTN_TODAY, TEXT_BTN_WEEK, TEXT_BTN_MONTH, TEXT_BTN_MENU):
        return

    # удалим сообщение-триггер (если есть права) — чтобы чат не мусорился
    try:
        await context.application.bot.delete_message(chat_id=chat_id, message_id=update.effective_message.message_id)
    except Exception:
        pass

    # показываем/обновляем панель
    if text == TEXT_BTN_TODAY:
        if not await _ensure_tasks_env(update):
            return
        await _show_today_panel(context.application, chat_id)
    elif text == TEXT_BTN_WEEK:
        if not await _ensure_tasks_env(update):
            return
        await _show_week_panel(context.application, chat_id)
    elif text == TEXT_BTN_MONTH:
        if not await _ensure_tasks_env(update):
            return
        await _show_month_panel(context.application, chat_id)
    elif text == TEXT_BTN_MENU:
        await _panel_set(context.application, chat_id, "Меню бота:", _main_menu_kb())

# ==================== ТЕСТ / ЭФИР ====================
async def cmd_test1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Эмулирует старт эфира: берём превью YouTube (если есть), иначе статичную картинку,
    постим анонс и запускаем почасовой пинг.
    """
    global current_live_stream_id, hourly_ping_task
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await _announce_with_sources(context.application, title, yt_live)

    # поднимем почасовой пинг как при реальном старте
    current_live_stream_id = f"test-{int(time.time())}"
    if HOURLY_PING_ENABLED:
        if hourly_ping_task and not hourly_ping_task.done():
            hourly_ping_task.cancel()
        hourly_ping_task = asyncio.create_task(_hourly_live_pinger(context.application))

# ==================== LOOP: твич, напоминания ====================
_posted_reminders_guard: set[str] = set()  # YYYY-MM-DD|HH:MM

async def _post_daily_reminder_if_due(app: Application):
    """Пост о сегодняшних стримах в REMINDER_CHAT_IDS в запланированное время (если сегодня есть стримы)."""
    now = now_local()
    hhmm = now.strftime("%H:%M")
    if hhmm not in SCHEDULE_REMINDER_TIMES:
        return
    key = f"{now.strftime('%Y-%m-%d')}|{hhmm}"
    if key in _posted_reminders_guard:
        return

    # есть ли сегодня стримы?
    if not all([GOOGLE_TASKS_CLIENT_ID, GOOGLE_TASKS_CLIENT_SECRET, GOOGLE_TASKS_REFRESH_TOKEN, GOOGLE_TASKS_LIST_ID]):
        return
    tasks = _tasks_fetch_all()
    today = now.date()
    todays = [t for t in tasks if _due_to_local_date(t.get("due") or "") == today]
    if not todays:
        _posted_reminders_guard.add(key)
        return

    # собираем простой не-моно текст
    lines = ["<b>📌 Стримы сегодня:</b>", ""]
    # сортировка — по времени в заголовке
    todays.sort(key=lambda t: (_extract_time_from_title(t.get("title") or "")[0] or "99:99"))
    for t in todays:
        hhmm, cleaned = _extract_time_from_title(t.get("title") or "")
        if hhmm:
            lines.append(f"• {hhmm} — {html_escape(cleaned)}")
        else:
            lines.append(f"• {html_escape(cleaned)}")
    lines.append("")
    lines.append("Залетай на стримчики! 🙌")

    text = "\n".join(lines)
    # только одна кнопка — «Вступить в клан»
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")]])

    for chat_id in REMINDER_CHAT_IDS:
        try:
            # если есть картинка — шлём как фото с подписью
            if SCHEDULE_REMINDER_IMAGE_URL:
                await app.bot.send_photo(chat_id=chat_id, photo=SCHEDULE_REMINDER_IMAGE_URL, caption=text, parse_mode="HTML", reply_markup=kb)
            else:
                await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            print(f"[REMINDER] send error to {chat_id}: {e}")

    _posted_reminders_guard.add(key)

async def minute_loop(app: Application):
    global current_live_stream_id, hourly_ping_task
    print(f"[WAKE] loop started at {now_local().isoformat()}")
    while True:
        try:
            # 1) Twitch: раз в минуту ищем новый эфир
            if _sec_since(_last_called_ts["tw_check"]) >= 60:
                tw_new = twitch_check_new_live()
                if tw_new:
                    # новый эфир -> анонс + почасовой пинг
                    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
                    title = tw_new.get("title") or (yt_live.get("title") if yt_live else "Стрим")
                    await _announce_with_sources(app, title, yt_live)

                    current_live_stream_id = tw_new["id"]
                    if HOURLY_PING_ENABLED:
                        if hourly_ping_task and not hourly_ping_task.done():
                            hourly_ping_task.cancel()
                        hourly_ping_task = asyncio.create_task(_hourly_live_pinger(app))

                _last_called_ts["tw_check"] = int(time.time())

            # 2) Ежедневные напоминания в заданные времена
            await _post_daily_reminder_if_due(app)

        except Exception as e:
            print(f"[WAKE] loop error: {e}")

        await asyncio.sleep(5)

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
    # 1) Команды (в списке — только публичные)
    await app.bot.set_my_commands([
        BotCommand("today", "📅 Стримы сегодня"),
        BotCommand("week", "🗓 Стримы на неделю"),
        BotCommand("month", "📆 Стримы за месяц (по неделям)"),
        BotCommand("menu", "Открыть меню"),
        # test1 намеренно НЕ добавляем
    ])

    # 2) Разошлём панельное сообщение в «целевые» чаты анонсов
    for chat_id in STREAM_CHAT_IDS:
        try:
            m = await app.bot.send_message(chat_id=chat_id, text="Меню бота:", reply_markup=_main_menu_kb())
            PANEL_MSG_IDS[int(chat_id)] = m.message_id
            # и отдадим Reply-клавиатуру
            await app.bot.send_message(chat_id=chat_id, text="Клавиатура:", reply_markup=build_main_reply_kb())
        except Exception as e:
            print(f"[START] cannot init panel in {chat_id}: {e}")

    # 3) Фоновые задачи
    asyncio.create_task(minute_loop(app))
    asyncio.create_task(self_ping())
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")

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

# ==================== ROUTING & MAIN ====================
def main():
    if not TG_TOKEN or not STREAM_CHAT_IDS:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS (or TELEGRAM_BOT_IDS) in ENV")
    if not PUBLIC_URL:
        raise SystemExit("Set PUBLIC_URL (https://<your-host>) for webhook (или используйте RENDER_EXTERNAL_URL)")

    application = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(_on_start)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("test1", cmd_test1))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week", cmd_week))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("menu", cmd_menu))

    # Текстовые кнопки (ReplyKeyboard) — ТОЛЬКО точные совпадения
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_buttons))

    # Inline callbacks
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
