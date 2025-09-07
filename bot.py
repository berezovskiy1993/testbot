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

# Базовый список «куда постим» — можно править прямо тут или через ENV
_raw_default = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
DEFAULT_CHAT_IDS = [c.strip() for c in _raw_default.split(",") if c.strip()]

# Раздельные списки: куда постить анонсы стримов и куда — напоминания
_raw_stream = (os.getenv("STREAM_CHAT_IDS") or "").strip()
STREAM_POST_CHAT_IDS = [c.strip() for c in _raw_stream.split(",") if c.strip()] or DEFAULT_CHAT_IDS

_raw_remind = (os.getenv("REMINDER_CHAT_IDS") or "").strip()
REMINDER_POST_CHAT_IDS = [c.strip() for c in _raw_remind.split(",") if c.strip()] or STREAM_POST_CHAT_IDS

# Киев: летом UTC+3, зимой UTC+2 — вручную
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# === Google Tasks (для расписания) ===
GOOGLE_TASKS_CLIENT_ID = os.getenv("GOOGLE_TASKS_CLIENT_ID", "").strip()
GOOGLE_TASKS_CLIENT_SECRET = os.getenv("GOOGLE_TASKS_CLIENT_SECRET", "").strip()
GOOGLE_TASKS_REFRESH_TOKEN = os.getenv("GOOGLE_TASKS_REFRESH_TOKEN", "").strip()
GOOGLE_TASKS_LIST_ID = os.getenv("GOOGLE_TASKS_LIST_ID", "").strip()

# YouTube (для превью по триггеру Twitch)
YT_API_KEY = os.getenv("YT_API_KEY", "").strip()
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "").strip()

# Twitch
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME", "dektrian_tv").strip()

# Картинка по умолчанию (если нет превью YouTube)
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()

# Картинка для напоминалок расписания (легко заменить)
REMINDER_IMAGE_URL = os.getenv("REMINDER_IMAGE_URL", "https://ibb.co/LXSMV1FQ").strip()

# Соцсети (переопределяемые через ENV при желании)
SOC_YT = "https://www.youtube.com/@Dektrian_TV"
SOC_TWITCH = "https://www.twitch.tv/dektrian_tv"
SOC_TG_GROUP = "https://t.me/dektrian_tv"
SOC_TG_CHANNEL = "https://t.me/dektrian_family"
SOC_TIKTOK = "https://www.tiktok.com/@dektrian_tv"

# Параметры вебхука
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "8080")))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "dektrian-secret")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", f"/telegram/{BOT_NAME}")

# ===== Напоминалки по расписанию (локальное время Europe/Kyiv) =====
REMINDER_TIMES = ["10:00", "14:00"]  # легко менять список

# ===== Почасовые “мы всё ещё на стриме” =====
LIVE_BUMP_INTERVAL_MIN = 60
TEST_BUMP_HOURS = 3  # сколько часов в /test1 слать “мы всё ещё онлайн”

# ========= In-memory state =========
last_twitch_stream_id: str | None = None
_tw_token: str | None = None
_tw_token_expire_at: int = 0
_last_called_ts = {"tw": 0}

_live_bump_task: asyncio.Task | None = None
_live_bump_stream_id: str | None = None

# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _parse_hhmm_local(s: str) -> tuple[int, int]:
    hh, mm = s.split(":")
    return int(hh), int(mm)

# ==================== TELEGRAM ====================
def build_keyboard(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    yt_url = (f"https://www.youtube.com/watch?v={youtube_video_id}"
              if youtube_video_id else
              (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID
               else "https://www.youtube.com/@dektrian_tv"))
    tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❤️ Гоу на YouTube", url=yt_url),
         InlineKeyboardButton("💜 Гоу на Twitch",  url=tw_url)],
        [InlineKeyboardButton("💸 Гоу Донатик", url="https://new.donatepay.ru/@Dektrian_TV"),
         InlineKeyboardButton("🤙 Гоу в клан", url="https://t.me/D13_join_bot")]
    ])

def reminder_kb_only_clan() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤙 Вступить в клан", url="https://t.me/D13_join_bot")]
    ])

def main_reply_kb() -> ReplyKeyboardMarkup:
    # строгое совпадение текста — чтобы не триггерилось на случайные слова
    rows = [
        [KeyboardButton("📺 Стрим сегодня"), KeyboardButton("📺 Стримы неделя")],
        [KeyboardButton("📺 Стримы месяц"), KeyboardButton("☰ Меню")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, one_time_keyboard=False)

# ==================== GOOGLE TASKS ====================
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

def _truncate_mono(s: str, width: int) -> str:
    # не сокращаем слова — только мягкое обрезание по ширине
    if len(s) <= width:
        return s
    return s[: max(0, width - 1)].rstrip() + "…"

def _format_today_compact(tasks: list[dict], d: date) -> str:
    """
    Компактный моно-вид:
    Заголовок с датой, внутри <pre> только 2 колонки: Время | Событие
    """
    header = f"📅 Сегодня — {d.strftime('%d.%m.%Y')}"
    lines = [html_escape(header), "<pre>", "Время  Событие", "------ --------------------------------"]
    todays = [t for t in tasks if _due_to_local_date(t.get("due") or "") == d]
    if not todays:
        lines.append("--     нет стримов")
    else:
        todays_sorted = sorted(
            todays,
            key=lambda t: (_extract_time_from_title(t.get('title') or "")[0] or "99:99")
        )
        for t in todays_sorted:
            hhmm, title = _extract_time_from_title(t.get("title") or "")
            time_str = hhmm or "--"
            # ширину подбирали так, чтобы хорошо смотрелось на мобиле
            lines.append(f"{time_str:<6} {_truncate_mono(html_escape(title), 30)}")
    lines.append("</pre>")
    return "\n".join(lines)

def _format_table_for_range(tasks: list[dict], start: date, end: date, title: str) -> str:
    m = _tasks_by_date_map(tasks)
    lines = [html_escape(title), "<pre>", "Дата     Дн  Время  Событие", "------- ---- ------ ---------------"]
    for d in _daterange_days(start, end):
        day = d.strftime("%d.%m"); wd = _weekday_abr(d)
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
                lines.append(f"{day:8} {wd:3} {time_str:5}  {html_escape(_truncate_mono(cleaned_title, 25))}")
                first = False
            else:
                lines.append(f"{'':8} {'':3} {time_str:5}  {html_escape(_truncate_mono(cleaned_title, 25))}")
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

def _tw_is_live_now() -> bool:
    """Быстрая проверка — онлайн ли прямо сейчас Twitch."""
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
async def tg_broadcast_photo_first(chat_ids: list[str], app: Application, text: str,
                                   kb: InlineKeyboardMarkup | None, photo_url: str):
    for chat_id in chat_ids:
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
    kb = build_keyboard(yt_id)
    await tg_broadcast_photo_first(STREAM_POST_CHAT_IDS, app, text, kb, photo_url)

async def _hourly_live_bump_loop(app: Application, test_hours: int | None = None):
    """
    Раз в час — «Мы всё ещё на стриме…», пока онлайн (или test_hours отработают).
    """
    global _live_bump_task
    i = 0
    while True:
        if test_hours is None:
            if not _tw_is_live_now():
                print("[BUMP] offline detected, stop bumps")
                break
        else:
            if i >= max(0, test_hours):
                break

        # Берём свежую ссылку YouTube (если есть)
        yt_live = await yt_fetch_live_with_retries(max_attempts=2, delay_seconds=5)
        yt_id = yt_live["id"] if yt_live else None
        yt_url = (f"https://www.youtube.com/watch?v={yt_id}"
                  if yt_id else (f"https://www.youtube.com/channel/{YT_CHANNEL_ID}" if YT_CHANNEL_ID
                                 else "https://www.youtube.com/@dektrian_tv"))
        tw_url = f"https://www.twitch.tv/{TWITCH_USERNAME}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❤️ YouTube", url=yt_url),
                                    InlineKeyboardButton("💜 Twitch", url=tw_url)]])

        text = "Мы всё ещё на стриме, врывайся! 😏"
        try:
            for chat_id in STREAM_POST_CHAT_IDS:
                await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        except Exception as e:
            print(f"[BUMP] send error: {e}")

        i += 1
        await asyncio.sleep(LIVE_BUMP_INTERVAL_MIN * 60)

    _live_bump_task = None

def _ensure_bump_started(app: Application, for_test: bool = False):
    global _live_bump_task
    if _live_bump_task is None or _live_bump_task.done():
        _live_bump_task = asyncio.create_task(
            _hourly_live_bump_loop(app, test_hours=(TEST_BUMP_HOURS if for_test else None))
        )

# ==================== ЦИКЛЫ ====================
async def minute_loop(app: Application):
    print(f"[WAKE] minute loop started at {now_local().isoformat()}")
    while True:
        try:
            if _sec_since(_last_called_ts["tw"]) >= 60:
                tw = twitch_check_live()
                if tw:
                    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
                    title = tw.get("title") or (yt_live.get("title") if yt_live else "Стрим")
                    await _announce_with_sources(app, title, yt_live)
                    _ensure_bump_started(app, for_test=False)
                _last_called_ts["tw"] = int(time.time())
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

async def reminders_loop(app: Application):
    """
    Дважды в день шлём расписание на сегодня (если есть задачи).
    Времена задаются в REMINDER_TIMES (локально, Киев).
    """
    already: set[tuple[int, int, int]] = set()  # (Y, M, D*100 + HH)
    print("[REM] loop started")
    while True:
        try:
            now = now_local()
            y, m, d = now.year, now.month, now.day
            for t in REMINDER_TIMES:
                hh, mm = _parse_hhmm_local(t)
                key = (y, m, d * 100 + hh)
                if key in already:
                    continue
                # триггерим в пределах текущего часа первой минуты
                if now.hour == hh and now.minute >= mm:
                    # Есть ли стримы сегодня?
                    tasks = _tasks_fetch_all()
                    todays = [x for x in tasks if _due_to_local_date(x.get("due") or "") == now.date()]
                    if todays:
                        # красивый текст без <pre>, только список
                        lines = [f"📅 <b>Стримы сегодня — {now.strftime('%d.%m.%Y')}</b>", ""]
                        todays_sorted = sorted(
                            todays, key=lambda t: (_extract_time_from_title(t.get('title') or "")[0] or "99:99")
                        )
                        for tsk in todays_sorted:
                            hhmm, ttl = _extract_time_from_title(tsk.get("title") or "")
                            time_str = hhmm or "--"
                            lines.append(f"▫️ {time_str} — {html_escape(ttl)}")
                        lines.append("")
                        lines.append("Залетай на стримчики! 💥")

                        await tg_broadcast_photo_first(
                            REMINDER_POST_CHAT_IDS, app,
                            "\n".join(lines), reminder_kb_only_clan(), REMINDER_IMAGE_URL
                        )
                    else:
                        print("[REM] skipped (no tasks today)")
                    already.add(key)
            await asyncio.sleep(20)
        except Exception as e:
            print(f"[REM] loop error: {e}")
            await asyncio.sleep(10)

# ==================== КНОПКИ/МЕНЮ ====================
def _main_menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
         InlineKeyboardButton("🗓 Неделя", callback_data="t|week")],
        [InlineKeyboardButton("📆 Месяц", callback_data="t|month"),
         InlineKeyboardButton("Соцсети стримера", callback_data="menu|socials")],
        [InlineKeyboardButton("Бронь стрима", url="https://t.me/DektrianTV"),
         InlineKeyboardButton("Купить юси", url="https://t.me/uc_pubg_bounty")],
        [InlineKeyboardButton("Вступить в клан", url="https://t.me/D13_join_bot")]
    ])

def _socials_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("YouTube", url=SOC_YT),
         InlineKeyboardButton("Twitch", url=SOC_TWITCH)],
        [InlineKeyboardButton("Группа Telegram", url=SOC_TG_GROUP),
         InlineKeyboardButton("Канал Telegram", url=SOC_TG_CHANNEL)],
        [InlineKeyboardButton("TikTok", url=SOC_TIKTOK),
         InlineKeyboardButton("← Назад", callback_data="menu|main")]
    ])

def _nav_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← В меню", callback_data="menu|main")],
                                 [InlineKeyboardButton("📅 Сегодня", callback_data="t|today"),
                                  InlineKeyboardButton("🗓 Неделя", callback_data="t|week"),
                                  InlineKeyboardButton("📆 Месяц", callback_data="t|month")]])

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
        [InlineKeyboardButton("← В меню", callback_data="menu|main")]
    ])

# ==================== КОМАНДЫ ====================
async def _ensure_tasks_env(update: Update) -> bool:
    if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
        if update.effective_message:
            await update.effective_message.reply_text(
                "❗ Не настроен доступ к Google Tasks. "
                "Нужны GOOGLE_TASKS_CLIENT_ID / SECRET / REFRESH_TOKEN / LIST_ID.",
                reply_markup=main_reply_kb(),
            )
        return False
    return True

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    tasks = _tasks_fetch_all()
    d = now_local().date()
    text = _format_today_compact(tasks, d)
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

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text("Меню бота:", reply_markup=_main_menu_inline())

async def cmd_test1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Имитация старта стрима: анонс + почасовые напоминания (TEST_BUMP_HOURS)."""
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await _announce_with_sources(context.application, title, yt_live)
    _ensure_bump_started(context.application, for_test=True)
    if update.effective_message:
        await update.effective_message.reply_text("Тест: отправил анонс (режим имитации).", reply_markup=main_reply_kb())

# ======= Обработка reply-кнопок (точное совпадение текста) =======
BTN_TEXT_TODAY  = "📺 Стрим сегодня"
BTN_TEXT_WEEK   = "📺 Стримы неделя"
BTN_TEXT_MONTH  = "📺 Стримы месяц"
BTN_TEXT_MENU   = "☰ Меню"

async def on_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_message.text:
        return
    text = update.effective_message.text.strip()
    if text == BTN_TEXT_TODAY:
        await cmd_today(update, context)
    elif text == BTN_TEXT_WEEK:
        await cmd_week(update, context)
    elif text == BTN_TEXT_MONTH:
        await cmd_month(update, context)
    elif text == BTN_TEXT_MENU:
        await cmd_menu(update, context)

# ======= Инлайн-меню: всё в одном сообщении =======
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    if data == "menu|main":
        try:
            await q.edit_message_text("Меню бота:", reply_markup=_main_menu_inline())
        except BadRequest:
            await q.edit_message_reply_markup(reply_markup=_main_menu_inline())
        return
    if data == "menu|socials":
        try:
            await q.edit_message_text("Соцсети стримера:", reply_markup=_socials_kb())
        except BadRequest:
            await q.edit_message_reply_markup(reply_markup=_socials_kb())
        return

    if data.startswith("t|"):
        # today/week/month — рендерим в этом же сообщении
        action = data.split("|", 1)[1]
        if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
            await q.edit_message_text("❗ Не настроен доступ к Google Tasks.", reply_markup=_nav_back_kb())
            return
        tasks = _tasks_fetch_all()
        if action == "today":
            d = now_local().date()
            text = _format_today_compact(tasks, d)
            try:
                await q.edit_message_text(text, parse_mode="HTML", reply_markup=_nav_back_kb())
            except BadRequest:
                await q.edit_message_reply_markup(reply_markup=_nav_back_kb())
        elif action == "week":
            start = now_local().date(); end = start + timedelta(days=6)
            text = _format_table_for_range(tasks, start, end, f"🗓 Неделя — {start.strftime('%d.%m')}–{end.strftime('%d.%m')}")
            try:
                await q.edit_message_text(text, parse_mode="HTML", reply_markup=_nav_back_kb())
            except BadRequest:
                await q.edit_message_reply_markup(reply_markup=_nav_back_kb())
        elif action == "month":
            today = now_local().date()
            year, month = today.year, today.month
            weeks = _month_weeks(year, month); idx = 0
            start, end = weeks[idx]
            text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
            kb = _month_kb(f"{year:04d}-{month:02d}", idx, len(weeks))
            try:
                await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            except BadRequest:
                await q.edit_message_reply_markup(reply_markup=kb)
        return

    if data.startswith("m|"):
        # month pager
        try:
            _, ym, idx_str = data.split("|")
            year, month = map(int, ym.split("-")); idx = int(idx_str)
        except Exception:
            return
        tasks = _tasks_fetch_all()
        weeks = _month_weeks(year, month)
        if not weeks:
            return
        idx = max(0, min(idx, len(weeks) - 1))
        start, end = weeks[idx]
        text = _format_table_for_range(tasks, start, end, _month_title(year, month, idx, len(weeks)))
        kb = _month_kb(ym, idx, len(weeks))
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except BadRequest:
            await q.edit_message_reply_markup(reply_markup=kb)

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
    # Публичные команды (без теста)
    await app.bot.set_my_commands([
        BotCommand("today", "📅 Стримы сегодня"),
        BotCommand("week", "🗓 Стримы на неделю"),
        BotCommand("month", "📆 Стримы за месяц"),
        BotCommand("menu", "Открыть меню"),
    ])

    # Покажем постоянную клавиатуру в целевых чатах (оставили как есть)
    for chat_id in DEFAULT_CHAT_IDS or STREAM_POST_CHAT_IDS:
        try:
            await app.bot.send_message(chat_id=chat_id, text="Меню бота:", reply_markup=main_reply_kb())
        except Exception as e:
            print(f"[STARTED] cannot show keyboard in {chat_id}: {e}")

    # Фоновые задачи
    asyncio.create_task(minute_loop(app))
    asyncio.create_task(self_ping())
    asyncio.create_task(reminders_loop(app))
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")

def main():
    if not TG_TOKEN or not (DEFAULT_CHAT_IDS or STREAM_POST_CHAT_IDS):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS or STREAM_CHAT_IDS in Environment")
    if not PUBLIC_URL:
        raise SystemExit("Set PUBLIC_URL (https://<your-host>) for webhook (или используйте RENDER_EXTERNAL_URL)")

    application = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(_on_start)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("test1", cmd_test1))   # скрытая
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week", cmd_week))
    application.add_handler(CommandHandler("month", cmd_month))
    application.add_handler(CommandHandler("menu", cmd_menu))

    # Текстовые кнопки (ReplyKeyboard) — строгое сравнение
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
