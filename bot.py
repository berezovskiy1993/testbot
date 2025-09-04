import os
import time
import asyncio
from datetime import datetime, timedelta, timezone
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict, TimedOut, NetworkError, BadRequest

BOT_NAME = "dektrian_online_bot"

# -------- ENV --------
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# дефолтные каналы по задаче
# DEFAULT_CHANNELS = ["@dektrian_family", "@dektrian_tv"]

_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
_env_chats = [c.strip() for c in _raw_chats.split(",") if c and c.strip()]

# uniq + сохранение порядка: сначала дефолтные, потом из ENV
_seen = set()
CHAT_IDS = []
for ch in DEFAULT_CHANNELS + _env_chats:
    if ch and ch not in _seen:
        CHAT_IDS.append(ch)
        _seen.add(ch)

# Киев: летом UTC+3, зимой UTC+2. Управляем вручную.
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# YouTube (используются по триггеру старта на Twitch)
YT_API_KEY = os.getenv("YT_API_KEY", "").strip()
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "").strip()

# Twitch
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME", "dektrian_tv").strip()

# Статичная картинка для постов (по умолчанию — твоя ссылка/страница).
# Важно: для стабильного send_photo лучше указывать прямой URL на изображение или file_id.
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()

# -------- In-memory state --------
last_twitch_stream_id: str | None = None

_tw_token: str | None = None
_tw_token_expire_at: int = 0  # unix ts

# когда последний раз дергали Twitch
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


# ==================== YOUTUBE ====================
def _yt_fetch_live_once() -> dict | None:
    """
    ОДНА попытка получить активный live на YouTube:
      - search.list (eventType=live) -> videoId, snippet.title
      - videos.list (part=snippet) -> лучшие thumbnails
    Возвращает dict {'id': videoId, 'title': title, 'thumb': best_thumb_url} или None.
    Стоимость: ~101 кв. ед. (100 + 1).
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
            # приоритет: maxres > standard > high > medium > default
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
        f"🔴 <b>Стрим начался! Забегай, я тебя жду :)</b>\n\n"
        f"<b>{title or ''}</b>\n\n"
        f"#DEKTRIAN #D13 #ОНЛАЙН"
    )
    kb = build_keyboard(yt_id)
    await tg_broadcast_photo_first(app, text, kb, photo_url)

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

async def scheduler(app: Application):
    """
    Раз в минуту опрашиваем Twitch.
    При детекте старта — 3 попытки (через 10 сек) подтянуть YouTube (линк + превью), затем пост.
    Дубликаты не шлём: проверка по stream_id.
    """
    print(f"[SCHED] started at {now_local().isoformat()}")
    while True:
        try:
            # --- Twitch раз в минуту ---
            if _sec_since(_last_called_ts["tw"]) >= 60:
                print(f"[SCHED] TW tick (interval=60s)")
                tw = twitch_check_live()
                if tw:
                    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
                    title = tw.get("title") or (yt_live.get("title") if yt_live else "Стрим")
                    await _announce_with_sources(app, title, yt_live)
                _last_called_ts["tw"] = int(time.time())

            await asyncio.sleep(5)  # короткий сон — не жрём CPU

        except Exception as e:
            print(f"[SCHED] loop error: {e}")
            await asyncio.sleep(10)


# ==================== КОМАНДЫ ====================
async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Тестовый пост с той же логикой превью:
      - пробуем 3× получить YouTube live и превью
      - если не нашли — берём статичную картинку
    """
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await _announce_with_sources(context.application, title, yt_live)
    try:
        await update.effective_message.reply_text("Тест: отправил анонс в целевые чаты/каналы.")
    except Exception:
        pass


# ==================== ERROR-HANDLER (для чистых логов на polling) ====================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        print("[POLLING] Conflict: другой процесс держит getUpdates. Жду и пробую снова...")
        await asyncio.sleep(5)
        return
    if isinstance(err, (TimedOut, NetworkError)):
        print(f"[POLLING] transient error: {err}")
        return
    print(f"[POLLING] unhandled error: {err}")


# ==================== STARTUP ====================
async def _on_start(app: Application):
    asyncio.create_task(scheduler(app))
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")


def main():
    if not TG_TOKEN or not CHAT_IDS:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS/TELEGRAM_CHANNEL_ID in Environment")

    application = Application.builder().token(TG_TOKEN).post_init(_on_start).build()

    application.add_handler(CommandHandler("test", cmd_test))
    application.add_error_handler(on_error)

    application.run_polling(
        close_loop=False,
        drop_pending_updates=True,
        allowed_updates=None
    )

if __name__ == "__main__":
    main()
