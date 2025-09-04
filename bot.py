import os
import time
import asyncio
import re
from datetime import datetime, timedelta, timezone, date  # ← добавлен date

import requests
import aiohttp  # нужен для self-ping
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict, TimedOut, NetworkError, BadRequest

BOT_NAME = "dektrian_online_bot"

# ========= ENV =========
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Постим ТОЛЬКО туда, что указано в переменной окружения (закрытые каналы/чаты ID формата -100...)
_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
CHAT_IDS = [c.strip() for c in _raw_chats.split(",") if c.strip()]

# Киев: летом UTC+3, зимой UTC+2 — вручную
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# === Google Tasks (для команд /today /week /next) ===
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

# Картинка для постов (желательно прямой URL на изображение)
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()

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

# ==================== УТИЛИТЫ ====================
def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def _sec_since(ts: int) -> int:
    return int(time.time()) - ts

# ==================== TELEGRAM ====================
def build_keyboard(youtube_video_id: str | None) -> InlineKeyboardMarkup:
    """
    Если знаем id активного YouTube-стрима — кнопка ведёт на его watch-URL.
    Иначе — на канал.
    """
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
         InlineKeyboardButton("🤙 Гоу в клан", url="https://t.me/D13_join_bot")]
    ])

async def tg_broadcast_photo_first(app: Application, text: str, kb: InlineKeyboardMarkup | None, photo_url: str):
    """
    Сначала пробуем отправить как фото (баннер). Если не вышло (непрямой URL и т.п.),
    фолбэк — обычное сообщение с включённым превью по ссылке.
    """
    for chat_id in CHAT_IDS:
        # 1) Фото
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

        # 2) Фолбэк: ссылка + текст (оставляем превью включённым)
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

# ==================== GOOGLE TASKS (helpers) ====================
def _tasks_get_access_token() -> str | None:
    """
    Берём access_token по refresh_token для Google Tasks API.
    """
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
    """
    Тянем все невыполненные задачи из указанного списка (страницы склеиваем).
    Каждая задача = all-day на дату due.
    """
    token = _tasks_get_access_token()
    if not token:
        return []
    items: list[dict] = []
    page_token = None
    try:
        while True:
            params = {
                "showCompleted": "false",
                "showDeleted": "false",
                "maxResults": "100",
            }
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

def _extract_time_from_title(title: str) -> tuple[str | None, str]:
    """
    Если в заголовке есть время (например '21:00 Призовые кастомы'), вернём ('21:00', 'Призовые кастомы').
    Иначе (None, оригинальный заголовок).
    """
    if not title:
        return None, "Без названия"
    m = _time_re.search(title)
    if not m:
        return None, title.strip()
    hhmm = f"{m.group(2)}:{m.group(3)}"
    # убираем leading-время + пробелы
    cleaned = title[:m.start()].strip() + " " + title[m.end():].strip()
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = title.strip()
    return hhmm, cleaned

def _due_to_local_date(due_iso: str) -> date | None:
    """
    Превращает due (например '2025-09-16T00:00:00.000Z') в локальную дату Europe/Kyiv.
    Для all-day это ок: получится та же дата в местном поясе.
    """
    if not due_iso:
        return None
    try:
        dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
        dt_local = dt.astimezone(timezone(timedelta(hours=TZ_OFFSET_HOURS)))
        return dt_local.date()
    except Exception:
        try:
            # запасной парсер по первым 10 символам (YYYY-MM-DD)
            return datetime.strptime(due_iso[:10], "%Y-%m-%d").date()
        except Exception:
            return None

def _format_tasks_list(tasks: list[dict], header: str) -> str:
    """
    Форматирует список задач стримов в читаемый текст.
    Показываем дату, при наличии времени в title — отдельным полем.
    """
    if not tasks:
        return f"{header}\n\nНет стримов в расписании."

    # Сортируем по дате (и времени, если есть в заголовке)
    def sort_key(t: dict):
        d = _due_to_local_date(t.get("due") or "")
        time_in_title, _ = _extract_time_from_title(t.get("title") or "")
        time_sort = time_in_title or "99:99"  # None -> в конец
        return (d or datetime(2100, 1, 1).date(), time_sort)

    tasks_sorted = sorted(tasks, key=sort_key)

    lines = [header, ""]
    for t in tasks_sorted:
        title = t.get("title") or "Без названия"
        d = _due_to_local_date(t.get("due") or "")
        date_str = d.strftime("%d.%m (%a)") if d else "без даты"
        hhmm, cleaned_title = _extract_time_from_title(title)
        if hhmm:
            lines.append(f"▫️ {date_str} {hhmm} — {cleaned_title}")
        else:
            lines.append(f"▫️ {date_str} — {cleaned_title}")
    return "\n".join(lines)

# ==================== YOUTUBE ====================
def _yt_fetch_live_once() -> dict | None:
    """
    ОДНА попытка получить активный live на YouTube.
    Возвращает dict {'id': videoId, 'title': title, 'thumb': best_thumb_url} или None.
    """
    if not (YT_API_KEY and YT_CHANNEL_ID):
        return None

    try:
        # 1) Ищем live
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "channelId": YT_CHANNEL_ID,
                "eventType": "live",
                "type": "video",
                "maxResults": 1,
                "order": "date",
                "key": YT_API_KEY,
            },
            timeout=20,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return None

        video_id = items[0]["id"]["videoId"]
        yt_title = items[0]["snippet"].get("title") or "LIVE on YouTube"

        # 2) Берём лучшие thumbnail из videos.list (snippet)
        r2 = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet",
                "id": video_id,
                "key": YT_API_KEY,
                "maxResults": 1,
            },
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
    """
    До max_attempts попыток с паузой delay_seconds.
    Возвращает dict {'id','title','thumb'} или None.
    """
    for attempt in range(1, max_attempts + 1):
        res = _yt_fetch_live_once()
        if res:
            return res
        if attempt < max_attempts:
            await asyncio.sleep(delay_seconds)
    return None

# ==================== TWITCH ====================
def _tw_fetch_token() -> str | None:
    """Получаем/обновляем app access token; держим expiry локально."""
    global _tw_token, _tw_token_expire_at
    now_ts = int(time.time())
    if _tw_token and now_ts < _tw_token_expire_at - 60:
        return _tw_token
    try:
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
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

# ==================== ОСНОВНАЯ ЛОГИКА ====================
async def _announce_with_sources(app: Application, title: str, yt_video: dict | None):
    """
    Собираем пост:
      - фото: превью YouTube (если есть) иначе STATIC_IMAGE_URL
      - кнопки: YouTube → на стрим, если знаем id; иначе на канал
    """
    yt_id = yt_video["id"] if yt_video else None
    photo_url = (yt_video.get("thumb") if (yt_video and yt_video.get("thumb")) else STATIC_IMAGE_URL)

    text = (
        "🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{title or ''}</b>\n\n"
        "#DEKTRIAN #D13 #ОНЛАЙН"
    )
    kb = build_keyboard(yt_id)
    await tg_broadcast_photo_first(app, text, kb, photo_url)

async def minute_loop(app: Application):
    """
    Самый простой «будильник»: каждые 60 сек проверяем Twitch.
    Работает и в режиме вебхуков, фоновой таской внутри процесса.
    """
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
                _last_called_ts["tw"] = int(time.time())
        except Exception as e:
            print(f"[WAKE] loop error: {e}")
        await asyncio.sleep(5)  # короткий сон, чтобы не жрать CPU

async def self_ping():
    """
    Периодический self-ping, чтобы Render (или другой бесплатный хост) не усыплял сервис.
    Раз в 10 минут дергаем PUBLIC_URL/_wake. 404 тоже засчитывается как трафик.
    """
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
        await asyncio.sleep(600)  # каждые 10 минут

# ==================== КОМАНДЫ ====================
async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Тестовый пост с логикой превью:
      - пробуем 3× получить YouTube live и превью
      - если не нашли — берём статичную картинку
    """
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await _announce_with_sources(context.application, title, yt_live)
    try:
        if update.effective_message:
            await update.effective_message.reply_text("Тест: отправил анонс в целевые чаты/каналы.")
    except Exception:
        pass

# ---- Новые команды с Google Tasks ----
async def _ensure_tasks_env(update: Update) -> bool:
    if not (GOOGLE_TASKS_CLIENT_ID and GOOGLE_TASKS_CLIENT_SECRET and GOOGLE_TASKS_REFRESH_TOKEN and GOOGLE_TASKS_LIST_ID):
        if update.effective_message:
            await update.effective_message.reply_text(
                "❗ Не настроен доступ к Google Tasks. "
                "Нужны GOOGLE_TASKS_CLIENT_ID / SECRET / REFRESH_TOKEN / LIST_ID в ENV."
            )
        return False
    return True

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    tasks = _tasks_fetch_all()
    today = now_local().date()
    todays = []
    for t in tasks:
        d = _due_to_local_date(t.get("due") or "")
        if d == today:
            todays.append(t)
    text = _format_tasks_list(todays, "📅 Стримы сегодня")
    if update.effective_message:
        await update.effective_message.reply_text(text)

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    tasks = _tasks_fetch_all()
    today = now_local().date()
    end = today + timedelta(days=7)
    weeks = []
    for t in tasks:
        d = _due_to_local_date(t.get("due") or "")
        if d and today <= d <= end:
            weeks.append(t)
    text = _format_tasks_list(weeks, "📅 Стримы на неделю")
    if update.effective_message:
        await update.effective_message.reply_text(text)

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_tasks_env(update):
        return
    tasks = _tasks_fetch_all()
    today = now_local().date()
    upcoming = []
    for t in tasks:
        d = _due_to_local_date(t.get("due") or "")
        if d and d >= today:
            upcoming.append(t)
    if upcoming:
        def sort_key(t: dict):
            d = _due_to_local_date(t.get("due") or "")
            time_in_title, _ = _extract_time_from_title(t.get("title") or "")
            time_sort = time_in_title or "99:99"
            return (d or datetime(2100, 1, 1).date(), time_sort)
        upcoming.sort(key=sort_key)
        next_list = [upcoming[0]]
    else:
        next_list = []
    text = _format_tasks_list(next_list, "📅 Ближайший стрим")
    if update.effective_message:
        await update.effective_message.reply_text(text)

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
    # Запускаем фоновые задачи
    asyncio.create_task(minute_loop(app))
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
    application.add_handler(CommandHandler("test", cmd_test))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week", cmd_week))
    application.add_handler(CommandHandler("next", cmd_next))
    application.add_error_handler(on_error)

    # Поднимаем встроенный веб-сервер и регистрируем вебхук в Telegram
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    print(f"[WEBHOOK] listen 0.0.0.0:{PORT}  path={WEBHOOK_PATH}  url={webhook_url}")

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,         # локальный путь, по которому будет принимать апдейты
        webhook_url=webhook_url,       # публичный URL для Telegram setWebhook
        secret_token=WEBHOOK_SECRET,   # X-Telegram-Bot-Api-Secret-Token
        drop_pending_updates=True,
        allowed_updates=None,
    )

if __name__ == "__main__":
    main()
