import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Bot, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/messages.db")
HERMES_BASE_URL = os.getenv("HERMES_BASE_URL", "https://api.openai.com/v1").rstrip("/")
HERMES_API_KEY = os.environ["HERMES_API_KEY"]
HERMES_MODEL = os.getenv("HERMES_MODEL", "Hermes-3-Llama-3.1-8B")

telegram_app = Application.builder().token(BOT_TOKEN).updater(None).build()


def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                chat_title TEXT NOT NULL,
                user_name TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_id, created_at)"
        )


def save_message(chat_id: int, chat_title: str, user_name: str, text: str) -> None:
    with get_db() as connection:
        connection.execute(
            "INSERT INTO messages (chat_id, chat_title, user_name, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, chat_title, user_name, text, datetime.now(timezone.utc).isoformat()),
        )


def read_messages(chat_id: int, hours: int) -> list[sqlite3.Row]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_db() as connection:
        return connection.execute(
            "SELECT user_name, text, created_at FROM messages WHERE chat_id = ? AND created_at >= ? ORDER BY created_at",
            (chat_id, since),
        ).fetchall()


def split_message(text: str, limit: int = 4000) -> list[str]:
    return [text[index : index + limit] for index in range(0, len(text), limit)] or [""]


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat or not update.effective_user:
        return False
    member = await context.bot.get_chat_member(
        update.effective_chat.id, update.effective_user.id
    )
    return member.status in {"administrator", "creator"}


async def collect_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    if message.text and not message.text.startswith("/"):
        save_message(
            chat.id,
            chat.title or "Telegram group",
            user.full_name,
            message.text.strip(),
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Bot aktif. Admin bisa memakai /summary untuk merangkum percakapan 24 jam terakhir."
    )


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    if update.effective_chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("Gunakan perintah ini di dalam grup.")
        return
    if not await is_admin(update, context):
        await update.effective_message.reply_text("Hanya admin grup yang bisa meminta ringkasan.")
        return

    hours = 24
    if context.args and context.args[0].isdigit():
        hours = max(1, min(int(context.args[0]), 168))
    messages = read_messages(update.effective_chat.id, hours)
    if not messages:
        await update.effective_message.reply_text(f"Belum ada pesan dalam {hours} jam terakhir.")
        return

    transcript = "\n".join(
        f"[{row['created_at']}] {row['user_name']}: {row['text']}" for row in messages
    )
    await update.effective_message.reply_text("Sedang membuat ringkasan...")
    try:
        summary = await summarize_with_hermes(transcript, hours)
    except Exception:
        logger.exception("Hermes request failed")
        await update.effective_message.reply_text(
            "Gagal menghubungi Hermes. Coba lagi beberapa saat lagi."
        )
        return

    for part in split_message(summary):
        await update.effective_message.reply_text(part)


async def summarize_with_hermes(transcript: str, hours: int) -> str:
    prompt = (
        f"Ringkas percakapan grup Telegram berikut untuk {hours} jam terakhir dalam bahasa Indonesia. "
        "Susun dengan bagian: topik utama, keputusan, tugas atau tindak lanjut, dan pertanyaan terbuka. "
        "Jangan mengarang informasi yang tidak ada.\n\n" + transcript
    )
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{HERMES_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {HERMES_API_KEY}"},
            json={
                "model": HERMES_MODEL,
                "temperature": 0.2,
                "messages": [
                    {
                        "role": "system",
                        "content": "Anda adalah asisten yang teliti dalam merangkum percakapan.",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
    return payload["choices"][0]["message"]["content"].strip()


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("summary", summary_command))
telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, collect_message)
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(
        url=f"{WEBHOOK_URL}/telegram/webhook",
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info("Telegram webhook configured")
    yield
    await telegram_app.bot.delete_webhook()
    await telegram_app.stop()
    await telegram_app.shutdown()


api = FastAPI(title="Hermes Telegram Summarizer", lifespan=lifespan)


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@api.post("/telegram/webhook")
async def telegram_webhook(
    request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(default=None)
) -> dict[str, bool]:
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request.json()
    update = Update.de_json(payload, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
