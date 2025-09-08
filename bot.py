# bot.py
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

# ========= ENV =========
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# БАЗОВЫЙ список чатов (если нужны по умолчанию)
_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
CHAT_IDS = [c.strip() for c in _raw_chats.split(",") if c.strip()]

# Отдельные списки для постинга (правьте здесь при желании «жёстко»):
# Если оставить пустыми, ниже они возьмут значения из ENV / CHAT_IDS.
STREAM_POST_CHAT_IDS = [c.strip() for c in (os.getenv("TELEGRAM_STREAM_CHAT_IDS") or "").split(",") if c.strip()] or CHAT_IDS
DAILY_POST_CHAT_IDS  = [c.strip() for c in (os.getenv("TELEGRAM_DAILY_CHAT_IDS") or "").split(",") if c.strip()] or CHAT_IDS

# Киев: летом UTC+3, зимой UTC+2 — вручную
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# === Google Tasks (для расписания) ===
GOOGLE_TASKS_CLIENT_ID = os.getenv("GOOGLE_TASKS_CLIENT_ID", "").strip()
GOOGLE_TASKS_CLIENT_SECRET = os.getenv("GOOGLE_TASKS_CLIENT_SECRET", "").strip()
GOOGLE_TASKS_REFRESH_TOKEN = os.getenv("GOOGLE_TASKS_REFRESH_TOKEN", "").strip()
GOOGLE_TASKS_LIST_ID = os.getenv("GOOGLE_TASKS_LIST_ID", "").strip()

# YouTube (для превью по лайву Twitch)
YT_API_KEY = os.getenv("YT_API_KEY", "").strip()
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "").strip()

# Twitch
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME", "dektrian_tv").strip()

# Картинки
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()  # баннер по умолчанию для анонса
DAILY_SCHEDULE_IMAGE_URL = os.getenv("DAILY_SCHEDULE_IMAGE_URL", "https://ibb.co/LXSMV1FQ").strip()  # картинка для дневных напоминаний

# Соцсети (используем из ENV или дефолты)
SOC_YT = os.getenv("SOCIAL_YOUTUBE", "https://www.youtube.com/@Dektrian_TV").strip()
SOC_TWITCH = os.getenv("SOCIAL_TWITCH", f"https://www.twitch.tv/{TWITCH_USERNAME}").strip()
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

# Персональные «якоря» сообщений меню/расписаний: (chat_id, user_id) -> message_id
_user_anchors: dict[tuple[int, int], int] = {}

# Почасовые напоминалки (по активному стриму)
_hourly_task: asyncio.Task | None = None
_hourly_stream_id: str | None = None
_hourly_interval_min: int = int(os.getenv("HOURLY_REMINDER_INTERVAL_MIN", "60"))
_hourly_last_msg_id: dict[str, int] = {}  # per chat_id -> message_id

# Дневные напоминания (только если есть задачи сегодня)
DAILY_TIMES = [t.strip() for t in os.getenv("DAILY_REMINDER_TIMES", "10:00,14:00").split(",") if t.strip()]
_daily_sent_flags: set[str] = set()  # ключи вида "YYYY-MM-DD|HH:MM"

# Метка для клавиатуры
KB_LABEL = "📅 Расписание стримов и прочее"

# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _local_today() -> date:
    return now_local().date()

# ==================== TELEGRAM UI ====================
def reply_keyboard() -> ReplyKeyboardMarkup:
    # одна широкая кнопка
    rows = [[KeyboardButton(KB_LABEL)]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, one_time_keyboard=False)

def main_menu_kb() -> InlineKeyboardMarkup:
    # 2 колонки
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
         InlineKeyboardButton("📅 Неделя",  callback_data="t|week")],
        [InlineKeyboardButton("📅 Месяц",   callback_data="t|month"),
         InlineKeyboardButton("Бронь стрима", callback_data="menu|booking")],
        [InlineKeyboardButton("Купить юси", url="https://t.me/uc_pubg_bounty"),
         InlineKeyboardButton("Вступить в клан", url="https://t.me/D13_join_bot")],
        [InlineKeyboardButton("Соцсети стримера", callback_data="menu|socials"),
         InlineKeyboardButton("← Закрыть", callback_data="menu|close")],
    ])

def socials_kb() -> InlineKeyboardMarkup:
    # 2 колонки
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("YouTube", url=SOC_YT),
         InlineKeyboardButton("Twitch", url=SOC_TWITCH)],
        [InlineKeyboardButton("Группа Telegram", url=SOC_TG_GROUP),
         InlineKeyboardButton("Канал Telegram",  url=SOC_TG_CHANNEL)],
        [InlineKeyboardButton("TikTok", url=SOC_TIKTOK)],
        [InlineKeyboardButton("← Назад", callback_data="menu|main")],
    ])

def booking_kb() -> InlineKeyboardMarkup:
    # 2 колонки
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Условия брони", callback_data="booking|rules"),
         InlineKeyboardButton("Сделать бронь", url="https://t.me/DektrianTV")],
        [InlineKeyboardButton("← Назад", callback_data="menu|main")],
    ])

def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Назад в меню", callback_data="menu|main")]])

def month_nav_kb(ym: str, idx: int, total: int) -> InlineKeyboardMarkup:
    prev_idx = (idx - 1) % total
    next_idx = (idx + 1) % total
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️", callback_data=f"m|{ym}|{prev_idx}"),
         InlineKeyboardButton(f"Неделя {idx+1}/{total}", callback_data=f"m|{ym}|{idx}"),
         InlineKeyboardButton("▶️", callback_data=f"m|{ym}|{next_idx}")],
        [InlineKeyboardButton("← Назад в меню", callback_data="menu|main")],
    ])

def build_stream_buttons(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    yt_url = (
        f"https://www.youtube.com/watch?v={youtube_video_id}"
        if youtube_video_id else
        (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID else "https://www.youtube.com/@dektrian_tv")
    )
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ Гоу на YouTube", url=yt_url),
         InlineKeyboardButton("💜 Гоу на Twitch",  url=tw_url)]
    ])

def only_clan_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Вступить в клан", url="https://t.me/D13_join_bot")]])

# ==================== GOOGLE TASKS (helpers) ====================
def _tasks_ready() -> bool:
    ok = all([GOOGLE_TASKS_CLIENT_ID, GOOGLE_TASKS_CLIENT_SECRET, GOOGLE_TASKS_REFRESH_TOKEN, GOOGLE_TASKS_LIST_ID])
    if not ok:
        print("[TASKS] Missing env: CLIENT_ID/SECRET/REFRESH_TOKEN/LIST_ID")
    return ok

def _tasks_get_access_token() -> str | None:
    if not _tasks_ready():
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

def _weekday_abr(d: date) -> str:
    return d.strftime("%a")

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

# ---- форматирование таблиц с переносом по колонке «Событие» ----
def _wrap_text_words(s: str, maxw: int) -> list[str]:
    """Переносим по словам в пределах maxw."""
    words = s.split()
    if not words:
        return [""]
    lines, cur = [], ""
    for w in words:
        add = w if not cur else " " + w
        if len(cur) + len(add) <= maxw:
            cur += add
        else:
            if cur:
                lines.append(cur)
            # если само слово длиннее maxw — грубо резанём
            while len(w) > maxw:
                lines.append(w[:maxw])
                w = w[maxw:]
            cur = w
    if cur:
        lines.append(cur)
    return lines

def _format_table_for_range(tasks: list[dict], start: date, end: date, title: str) -> str:
    """
    Красивое моноширинное «табличное» представление с переносами.
    Колонки: Дата | Дн | Время | Событие
    Пустые даты -> "--" и "нет стримов"
    """
    m = _tasks_by_date_map(tasks)

    lines: list[str] = []
    lines.append(html_escape(title))
    lines.append("<pre>")
    header = "Дата     Дн  Время  Событие"
    sep    = "------- ---- ------ ---------------"
    lines.append(header)
    lines.append(sep)

    # вычислим ширину для колонки «Событие»
    # общая ширина ≈ 70, левый отступ до события:
    event_col = len("Дата     ") + len("Дн  ") + len("Время  ")
    total_w = 70
    event_w = max(10, total_w - event_col)

    for d in _daterange_days(start, end):
        day = d.strftime("%d.%m")
        wd = _weekday_abr(d)
        day_tasks = m.get(d, [])
        if not day_tasks:
            line = f"{day:8} {wd:3} {'--':5}  нет стримов"
            lines.append(line)
            continue

        # на день может быть несколько задач — отсортируем по времени в заголовке
        day_tasks_sorted = sorted(
            day_tasks,
            key=lambda t: (_extract_time_from_title(t.get('title') or "")[0] or "99:99")
        )
        first_row = True
        for t in day_tasks_sorted:
            hhmm, cleaned_title = _extract_time_from_title(t.get("title") or "")
            time_str = hhmm or "--"
            wrapped = _wrap_text_words(html_escape(cleaned_title), event_w)
            # первая строка — с датой/днём
            lines.append(f"{day:8} {wd:3} {time_str:5}  {wrapped[0]}")
            # последующие — с отступом под колонку «Событие»
            pad = " " * event_col
            for cont in wrapped[1:]:
                lines.append(f"{pad}{cont}")
            first_row = False
    lines.append("</pre>")
    return "\n".join(lines)

def _format_today_plain(tasks: list[dict], d: date) -> str:
    """Обычный текст (НЕ моно) для дневных напоминаний."""
    same_day = [t for t in tasks if _due_to_local_date(t.get("due") or "") == d]
    if not same_day:
        return ""
    # сортировка
    same_day.sort(key=lambda t: (_extract_time_from_title(t.get("title") or "")[0] or "99:99"))
    lines = ["<b>Стримы сегодня:</b>"]
    for t in same_day:
        hhmm, title = _extract_time_from_title(t.get("title") or "")
        if hhmm:
            lines.append(f"• <b>{hhmm}</b> — {html_escape(title)}")
        else:
            lines.append(f"• {html_escape(title)}")
    lines.append("")
    lines.append("Залетай на стримчики! 🔥")
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

        # получим лучший thumb
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

def twitch_check_new_stream() -> dict | None:
    """
    Возвращает {'id': stream_id, 'title': title} ТОЛЬКО когда обнаружен НОВЫЙ эфир, иначе None.
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
                return None
        print(f"[TW] streams HTTP {getattr(e.response, 'status_code', '?')}: {getattr(e.response, 'text', '')}")
    except Exception as e:
        print(f"[TW] error: {e}")
    return None

def twitch_is_live() -> tuple[bool, str | None, str | None]:
    """
    Проверяем, онлайн ли сейчас Twitch.
    Возвращаем (is_live, stream_id, title).
    """
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET and TWITCH_USERNAME):
        return (False, None, None)

    tk = _tw_fetch_token()
    if not tk:
        return (False, None, None)
    try:
        r = requests.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_login": TWITCH_USERNAME},
            headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {tk}"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return (False, None, None)
        s = data[0]
        return (True, s.get("id"), s.get("title"))
    except Exception as e:
        print(f"[TW] is_live error: {e}")
        return (False, None, None)

# ==================== ПОСТИНГ ====================
async def tg_send_photo_or_link(app: Application, chat_id: str | int, text: str, photo_url: str,
                                kb: InlineKeyboardMarkup | None, silent: bool):
    """
    Пробуем фото → фолбэк ссылка+текст.
    """
    try:
        await app.bot.send_photo(
            chat_id=chat_id,
            photo=photo_url,
            caption=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_notification=silent,
        )
        return
    except BadRequest as e:
        print(f"[TG] photo failed {chat_id}: {e}. Fallback to link.")
    except Exception as e:
        print(f"[TG] photo error {chat_id}: {e}. Fallback to link.")

    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"{photo_url}\n\n{text}",
            parse_mode="HTML",
            reply_markup=kb,
            disable_notification=silent,
        )
    except Exception as e:
        print(f"[TG] message send error {chat_id}: {e}")

async def broadcast_announce(app: Application, title: str, yt_video: dict | None):
    yt_id = yt_video["id"] if yt_video else None
    photo_url = (yt_video.get("thumb") if (yt_video and yt_video.get("thumb")) else STATIC_IMAGE_URL)
    text = (
        "🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{html_escape(title or 'Стрим')}</b>\n\n"
        "#DEKTRIAN #D13 #ОНЛАЙН"
    )
    kb = build_stream_buttons(yt_id)
    for chat_id in STREAM_POST_CHAT_IDS:
        await tg_send_photo_or_link(app, chat_id, text, photo_url, kb, silent=False)

async def hourly_reminders_loop(app: Application, stream_id: str):
    """
    Пока Twitch онлайн — каждые N минут шлем напоминалку в STREAM_POST_CHAT_IDS.
    Перед отправкой удаляем предыдущую напоминалку в каждом чате.
    """
    global _hourly_last_msg_id, _hourly_stream_id
    print(f"[REM] hourly loop started for stream {stream_id}")
    try:
        while True:
            live, sid, _ = twitch_is_live()
            if not live or sid != stream_id:
                print("[REM] stream offline or id changed — stop hourly loop")
                break

            text = "⏰ Мы всё ещё на стриме, врывайся! 😏"
            kb = build_stream_buttons(None)  # ссылки на YouTube/Twitch (YT — на канал, если лайв не нашли)

            # отправим и удалим прошлое
            for chat_id in STREAM_POST_CHAT_IDS:
                # delete previous
                prev_id = _hourly_last_msg_id.get(str(chat_id))
                if prev_id:
                    try:
                        await app.bot.delete_message(chat_id=chat_id, message_id=prev_id)
                    except Exception:
                        pass
                # send new
                try:
                    msg = await app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=kb,
                        disable_notification=False,  # со звуком
                    )
                    _hourly_last_msg_id[str(chat_id)] = msg.message_id
                except Exception as e:
                    print(f"[REM] send error to {chat_id}: {e}")

            await asyncio.sleep(max(1, _hourly_interval_min) * 60)
    finally:
        # очистка
        _hourly_last_msg_id = {}
        _hourly_stream_id = None
        print("[REM] hourly loop finished")

# ==================== РАСПИСАНИЕ (команды/меню) ====================
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
    ru_months = ["", "Январь","Февраль","Март","Апрель","Май","Июнь",
                 "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    return f"📅 {ru_months[month]} {year} — Неделя {idx+1}/{total}"

async def _ensure_anchor_and_send(app: Application, update: Update, text: str,
                                  kb: InlineKeyboardMarkup, silent: bool = True) -> None:
    """
    Логика якоря: на пользователя в чате один наш «рабочий» пост.
    Если есть — редактируем, если нет — создаём.
    Также удаляем триггер-сообщение пользователя (но не стартовую «клавиатурную» подсказку бота).
    """
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    key = (chat_id, user_id)
    msg_id = _user_anchors.get(key)

    # если пришло сообщение от пользователя (клавиатура), попробуем удалить его после ответа
    trigger_msg_id = getattr(update.effective_message, "message_id", None)

    if msg_id:
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except BadRequest:
            # если не удалось отредактировать (удалено/старое) — создадим заново
            try:
                m = await app.bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode="HTML", reply_markup=kb,
                    disable_notification=silent
                )
                _user_anchors[key] = m.message_id
            except Exception as e:
                print(f"[ANCHOR] send error: {e}")
        except Exception as e:
            print(f"[ANCHOR] edit error: {e}")
    else:
        try:
            m = await app.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode="HTML", reply_markup=kb,
                disable_notification=silent
            )
            _user_anchors[key] = m.message_id
        except Exception as e:
            print(f"[ANCHOR] send error: {e}")

    # подчистим триггер пользователя (если это не бот-сообщение)
    if trigger_msg_id:
        try:
            await app.bot.delete_message(chat_id=chat_id, message_id=trigger_msg_id)
        except Exception:
            pass

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ensure_anchor_and_send(context.application, update, "Меню бота:", main_menu_kb(), silent=True)

async def show_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = _tasks_fetch_all() if _tasks_ready() else []
    d = _local_today()
    text = _format_table_for_range(tasks, d, d, f"📅 Сегодня — {d.strftime('%d.%m.%Y')}")
    await _ensure_anchor_and_send(context.application, update, text, back_to_menu_kb(), silent=True)

async def show_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = _tasks_fetch_all() if _tasks_ready() else []
    start = _local_today()
    end = start + timedelta(days=6)
    text = _format_table_for_range(tasks, start, end, f"📅 Неделя — {start.strftime('%d.%m')}–{end.strftime('%d.%m')}")
    await _ensure_anchor_and_send(context.application, update, text, back_to_menu_kb(), silent=True)

async def show_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = _tasks_fetch_all() if _tasks_ready() else []
    today = _local_today()
    weeks = _month_weeks(today.year, today.month)
    idx = 0
    start, end = weeks[idx]
    text = _format_table_for_range(tasks, start, end, _month_title(today.year, today.month, idx, len(weeks)))
    kb = month_nav_kb(f"{today.year:04d}-{today.month:02d}", idx, len(weeks))
    await _ensure_anchor_and_send(context.application, update, text, kb, silent=True)

# ==================== CALLBACKS ====================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    # Определим «виртуальный» Update для якорной логики
    stub_update = Update(update.update_id, message=q.message)
    if data == "menu|main":
        await show_menu(stub_update, context)
        return
    if data == "menu|close":
        # Закрыть: удаляем якорь пользователя
        chat_id = q.message.chat.id
        user_id = q.from_user.id
        key = (chat_id, user_id)
        msg_id = _user_anchors.pop(key, None)
        if msg_id:
            try:
                await context.application.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        return
    if data == "menu|socials":
        await _ensure_anchor_and_send(context.application, stub_update, "Соцсети стримера:", socials_kb(), silent=True)
        return
    if data == "menu|booking":
        await _ensure_anchor_and_send(context.application, stub_update, "Бронь стрима:", booking_kb(), silent=True)
        return
    if data == "booking|rules":
        text = (
            "<b>Условия брони:</b>\n"
            "• Призовые кастомки — бесплатно при условии от 3 игр, приз 480 UC за карту, свободный вход.\n"
            "• Турниры / лиги / праки — от 250₽ / 125₴ за 1 катку (по договоренности).\n"
            "• TDM-турниры — от 100₽ / 50₴ за катку.\n"
        )
        await _ensure_anchor_and_send(context.application, stub_update, text, booking_kb(), silent=True)
        return
    if data.startswith("t|"):
        action = data.split("|", 1)[1]
        if action == "today":
            await show_today(stub_update, context)
        elif action == "week":
            await show_week(stub_update, context)
        elif action == "month":
            await show_month(stub_update, context)
        return
    if data.startswith("m|"):
        try:
            _, ym, idx_str = data.split("|")
            year, month = map(int, ym.split("-"))
            idx = int(idx_str)
        except Exception:
            return
        tasks = _tasks_fetch_all() if _tasks_ready() else []
        weeks = _month_weeks(year, month)
        if not weeks:
            return
        idx = max(0, min(idx, len(weeks)-1))
        start, end = weeks[idx]
        text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
        kb = month_nav_kb(ym, idx, len(weeks))
        # редактирование через якорь
        stub_update = Update(update.update_id, message=q.message)
        await _ensure_anchor_and_send(context.application, stub_update, text, kb, silent=True)
        return

# ==================== HANDLERS ====================
async def on_keyboard_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Нажатие на ReplyKeyboard («📅 Расписание стримов и прочее») — открываем меню в якоре.
    """
    if not update.effective_message or not update.effective_message.text:
        return
    if update.effective_message.text.strip() == KB_LABEL:
        await show_menu(update, context)

# Команды (латиница), test1 — в списке команд не показываем
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):  await show_today(update, context)
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):   await show_week(update, context)
async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):  await show_month(update, context)
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):   await show_menu(update, context)

async def cmd_test1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Имитация старта стрима: как при реальном старте — дергаем YouTube превью и шлём анонс.
    Дополнительно запустим почасовой цикл как будто эфир идёт.
    """
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await broadcast_announce(context.application, title, yt_live)

    # Запустим почасовые напоминания «симулированно» на 1 цикл через 5 секунд
    async def _one_shot():
        await asyncio.sleep(5)
        text = "⏰ Мы всё ещё на стриме, врывайся! 😏"
        kb = build_stream_buttons(yt_live["id"] if yt_live else None)
        for chat_id in STREAM_POST_CHAT_IDS:
            try:
                await context.application.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, disable_notification=False)
            except Exception as e:
                print(f"[TEST1] reminder send error {chat_id}: {e}")
    asyncio.create_task(_one_shot())

    if update.effective_message:
        try:
            await update.effective_message.reply_text("OK: отправил тестовый анонс.", disable_notification=True)
        except Exception:
            pass

# ==================== ПЛАНИРОВАНИЕ ====================
async def minute_loop(app: Application):
    """
    1) Раз в минуту проверяем новый Twitch-эфир -> анонс + запуск почасовых напоминаний.
    2) Ежедневные «Стримы сегодня» в заданные времена (если есть стримы).
    """
    global _hourly_task, _hourly_stream_id

    print(f"[WAKE] minute loop started at {now_local().isoformat()}")
    while True:
        try:
            # --- (1) Twitch новый эфир ---
            if _sec_since(_last_called_ts["tw"]) >= 60:
                tw = twitch_check_new_stream()
                if tw:
                    # анонс
                    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
                    title = tw.get("title") or (yt_live.get("title") if yt_live else "Стрим")
                    await broadcast_announce(app, title, yt_live)
                    # почасовой цикл
                    live, sid, _ = twitch_is_live()
                    if live and sid:
                        # убьём прежний (на всякий)
                        if _hourly_task and not _hourly_task.done():
                            _hourly_task.cancel()
                        _hourly_stream_id = sid
                        _hourly_task = asyncio.create_task(hourly_reminders_loop(app, sid))
                _last_called_ts["tw"] = int(time.time())

            # --- (2) Дневные напоминания ---
            local_now = now_local()
            today_str = local_now.strftime("%Y-%m-%d")
            hhmm = local_now.strftime("%H:%M")
            for tstr in DAILY_TIMES:
                # отрабатываем один раз на конкретное hh:mm в сутки
                key = f"{today_str}|{tstr}"
                if key in _daily_sent_flags:
                    continue
                if hhmm == tstr:
                    # тянем задачи и, если есть на сегодня, шлём
                    if _tasks_ready():
                        tasks = _tasks_fetch_all()
                        text = _format_today_plain(tasks, local_now.date())
                        if text:
                            for chat_id in DAILY_POST_CHAT_IDS:
                                # только кнопка «Вступить в клан», без моншрифта, можно с картинкой
                                if DAILY_SCHEDULE_IMAGE_URL:
                                    await tg_send_photo_or_link(app, chat_id, text, DAILY_SCHEDULE_IMAGE_URL, only_clan_kb(), silent=False)
                                else:
                                    try:
                                        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                                                   reply_markup=only_clan_kb(),
                                                                   disable_notification=False)
                                    except Exception as e:
                                        print(f"[DAILY] send error {chat_id}: {e}")
                        else:
                            print("[DAILY] no streams today — skip")
                    else:
                        print("[DAILY] tasks not configured — skip")
                    _daily_sent_flags.add(key)

        except Exception as e:
            print(f"[WAKE] loop error: {e}")

        await asyncio.sleep(5)

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
    # 1) Команды (латиница). test1 — скрытая, в список не добавляем.
    await app.bot.set_my_commands([
        BotCommand("today", "📅 Стримы сегодня"),
        BotCommand("week",  "📅 Стримы на неделю"),
        BotCommand("month", "📅 Стримы за месяц (по неделям)"),
        BotCommand("menu",  "Открыть меню"),
    ])

    # 2) Тихое стартовое сообщение в целевых чатах для включения постоянной клавиатуры
    for chat_id in STREAM_POST_CHAT_IDS:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text="Клавиатура активна. Нажмите кнопку ниже, чтобы открыть меню.",
                reply_markup=reply_keyboard(),
                disable_notification=True,  # без звука
            )
        except Exception as e:
            print(f"[STARTED] cannot show keyboard in {chat_id}: {e}")

    # 3) Фоновые задачи
    asyncio.create_task(minute_loop(app))
    asyncio.create_task(self_ping())
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")

# ==================== MAIN ====================
def main():
    if not TG_TOKEN or not (STREAM_POST_CHAT_IDS or CHAT_IDS):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_*_CHAT_IDS / TELEGRAM_CHAT_IDS in ENV or edit lists in code.")
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
    application.add_handler(CommandHandler("week",  cmd_week))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("menu",  cmd_menu))
    application.add_handler(CommandHandler("test1", cmd_test1))  # скрытая: не в /commands

    # Текст клавиатуры
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_keyboard_text))

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
