import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
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
    if not message or not chat or not user:
        return
    if message.text and not message.text.startswith("/"):
        if chat.type == ChatType.PRIVATE:
            chat_title = f"Private chat: {user.full_name}"
        elif chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            chat_title = chat.title or "Telegram group"
        else:
            return
        save_message(
            chat.id,
            chat_title,
            user.full_name,
            message.text.strip(),
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Bot aktif.\n\n"
        "Grup: admin gunakan /summary.\n"
        "Privat: forward chat penting ke sini, lalu gunakan /summary.\n"
        "Terjemahan: /translate en teks atau reply pesan dengan /translate id."
    )


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    if update.effective_chat.type in {ChatType.GROUP, ChatType.SUPERGROUP} and not await is_admin(update, context):
        await update.effective_message.reply_text("Hanya admin grup yang bisa meminta ringkasan.")
        return
    if update.effective_chat.type not in {
        ChatType.PRIVATE,
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
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


LANGUAGE_ALIASES = {
    "id": "Bahasa Indonesia",
    "indonesia": "Bahasa Indonesia",
    "en": "English",
    "inggris": "English",
    "de": "German",
    "jerman": "German",
    "fr": "French",
    "prancis": "French",
    "th": "Thai",
    "thailand": "Thai",
}


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Bantuan Hermes Bot\n\n"
        "Ringkasan:\n"
        "/summary - ringkas pesan 24 jam terakhir\n"
        "/summary 6 - ringkas pesan 6 jam terakhir\n"
        "/digest - alias /summary\n\n"
        "Translate:\n"
        "/translate en teks\n"
        "/translate Japanese teks\n"
        "Atau reply pesan lalu kirim /translate id\n"
        "Daftar kode bahasa:\n"
        "id = Indonesia\n"
        "en = Inggris\n"
        "ja = Jepang\n"
        "ko = Korea\n"
        "zh = Mandarin\n"
        "ar = Arab\n"
        "de = Jerman\n"
        "fr = Prancis\n"
        "es = Spanyol\n"
        "it = Italia\n"
        "pt = Portugis\n"
        "ru = Rusia\n"
        "hi = Hindi\n"
        "th = Thai\n"
        "vi = Vietnam\n"
        "nl = Belanda\n"
        "tr = Turki\n"
        "ms = Melayu\n\n"
        "Contoh: /translate ja Selamat pagi\n\n"
        "Mode privat:\n"
        "Forward pesan dari grup atau channel ke chat bot, lalu kirim /summary.\n\n"
        "Mode grup:\n"
        "Bot mengumpulkan teks setelah ditambahkan. Hanya admin yang dapat meminta summary."
    )


async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    target_key = context.args[0].lower() if context.args else ""
    target = LANGUAGE_ALIASES.get(target_key, context.args[0] if context.args else "")
    if not target or len(target) > 60:
        await message.reply_text(
            "Format: /translate <bahasa> <teks>\n"
            "Contoh: /translate English Halo dunia\n"
            "Bisa memakai kode atau nama bahasa, misalnya id, en, Japanese, Arab, Korean."
        )
        return

    source_text = " ".join(context.args[1:]).strip()
    if not source_text and message.reply_to_message:
        source_text = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not source_text:
        await message.reply_text("Tulis teks setelah nama bahasa atau reply pesan yang ingin diterjemahkan.")
        return

    try:
        translation = await translate_with_hermes(source_text, LANGUAGES[target])
    except Exception:
        logger.exception("Hermes translation request failed")
        await message.reply_text("Gagal menerjemahkan. Coba lagi beberapa saat lagi.")
        return
    await message.reply_text(translation)


async def summarize_with_hermes(transcript: str, hours: int) -> str:
    prompt = (
        f"Analisis percakapan Telegram berikut untuk {hours} jam terakhir. "
        "Abaikan sapaan, basa-basi, candaan, pengulangan, dan pesan tanpa informasi baru. "
        "Pertahankan hanya berita, fakta, perubahan penting, keputusan, tenggat, tugas, risiko, "
        "pertanyaan yang belum terjawab, atau informasi yang bisa membuat pembaca ketinggalan konteks. "
        "Jika tidak ada hal penting, katakan persis: Tidak ada informasi penting. "
        "Tulis dalam bahasa Indonesia dengan bagian: Ringkasan penting, Keputusan dan tugas, "
        "Berita atau perubahan, dan Pertanyaan terbuka. Jangan mengarang.\n\n" + transcript
    )
    return await hermes_completion(prompt, "Anda adalah editor berita yang teliti dan anti-halu.")


async def translate_with_hermes(text: str, target_language: str) -> str:
    prompt = (
        f"Terjemahkan teks berikut ke {target_language}. Pertahankan makna, nama, angka, "
        "tautan, dan format. Jangan beri penjelasan tambahan.\n\n{text}"
    )
    return await hermes_completion(prompt, "Anda adalah penerjemah profesional yang akurat.")


async def hermes_completion(prompt: str, system_message: str) -> str:
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
                        "content": system_message,
                    },
                    {"role": "user", "content": prompt},
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
    return payload["choices"][0]["message"]["content"].strip()


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CommandHandler("summary", summary_command))
telegram_app.add_handler(CommandHandler("digest", summary_command))
telegram_app.add_handler(CommandHandler("translate", translate_command))
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
