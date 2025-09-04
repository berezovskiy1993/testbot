import os
import time
import asyncio
from datetime import datetime, timedelta, timezone
import requests
import aiohttp  # нужен для self-ping
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict, TimedOut, NetworkError, BadRequest

BOT_NAME = "dektrian_online_bot"

# ========= ENV =========
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Чаты/каналы для постинга
_raw_chats = (os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
CHAT_IDS = [c.strip() for c in _raw_chats.split(",") if c.strip()]

# Часовой пояс
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))

# Google Tasks
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

# Картинка для постов
STATIC_IMAGE_URL = os.getenv("POST_IMAGE_URL", "https://ibb.co/V0RPnFx1").strip()

# Вебхук
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
    for chat_id in CHAT_IDS:
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

# ==================== GOOGLE TASKS ====================
def _get_google_tasks_access_token() -> str | None:
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

def fetch_tasks() -> list[dict]:
    token = _get_google_tasks_access_token()
    if not token:
        return []
    try:
        r = requests.get(
            f"https://tasks.googleapis.com/tasks/v1/lists/{GOOGLE_TASKS_LIST_ID}/tasks",
            headers={"Authorization": f"Bearer {token}"},
            params={"showCompleted": "false"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception as e:
        print(f"[TASKS] fetch error: {e}")
        return []

def _format_tasks(tasks: list[dict], header: str) -> str:
    if not tasks:
        return f"{header}\n\nНет стримов в расписании."
    lines = []
    for t in tasks:
        title = t.get("title", "Без названия")
        due = t.get("due")
        dt = None
        if due:
            try:
                dt = datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone(
                    timezone(timedelta(hours=TZ_OFFSET_HOURS)))
            except Exception:
                pass
        if dt:
            date_str = dt.strftime("%d.%m (%a)")
        else:
            date_str = "без даты"
        lines.append(f"▫️ {date_str} — {title}")
    return f"{header}\n\n" + "\n".join(lines)

# ==================== КОМАНДЫ ====================
async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    yt_live = await yt_fetch_live_with_retries(max_attempts=3, delay_seconds=10)
    title = (yt_live.get("title") if yt_live else f"Тестовый пост от {BOT_NAME}")
    await _announce_with_sources(context.application, title, yt_live)
    try:
        await update.effective_message.reply_text("Тест: отправил анонс в целевые чаты/каналы.")
    except Exception:
        pass

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = fetch_tasks()
    today = now_local().date()
    todays = []
    for t in tasks:
        due = t.get("due")
        if not due: continue
        dt = datetime.fromisoformat(due.replace("Z", "+00:00")).date()
        if dt == today:
            todays.append(t)
    text = _format_tasks(todays, "📅 Стримы сегодня")
    await update.message.reply_text(text)

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = fetch_tasks()
    today = now_local().date()
    week = today + timedelta(days=7)
    weeks = []
    for t in tasks:
        due = t.get("due")
        if not due: continue
        dt = datetime.fromisoformat(due.replace("Z", "+00:00")).date()
        if today <= dt <= week:
            weeks.append(t)
    text = _format_tasks(weeks, "📅 Стримы на неделю")
    await update.message.reply_text(text)

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = fetch_tasks()
    upcoming = []
    today = now_local().date()
    for t in tasks:
        due = t.get("due")
        if not due: continue
        dt = datetime.fromisoformat(due.replace("Z", "+00:00")).date()
        if dt >= today:
            upcoming.append((dt, t))
    upcoming.sort(key=lambda x: x[0])
    nexts = [upcoming[0][1]] if upcoming else []
    text = _format_tasks(nexts, "📅 Ближайший стрим")
    await update.message.reply_text(text)

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
    asyncio.create_task(minute_loop(app))
    asyncio.create_task(self_ping())
    print(f"[STARTED] {BOT_NAME} at {now_local().isoformat()}")

def main():
    if not TG_TOKEN or not CHAT_IDS:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS in Environment")
    if not PUBLIC_URL:
        raise SystemExit("Set PUBLIC_URL (https://<your-host>) for webhook")

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
