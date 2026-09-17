import logging
import html
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from telegram_user import (
    init_user_table,
    list_groups,
    delete_legacy_group_lists,
    new_client,
    read_group_messages,
    load_user_state,
    save_session,
    save_user_state,
)
from database import get_db, using_postgres

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/messages.db")
HERMES_BASE_URL = os.getenv("HERMES_BASE_URL", "https://api.openai.com/v1").rstrip("/")
HERMES_API_KEY = os.environ["HERMES_API_KEY"]
HERMES_MODEL = os.getenv("HERMES_MODEL", "Hermes-3-Llama-3.1-8B")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "Afung_12bot").lstrip("@")

telegram_app = Application.builder().token(BOT_TOKEN).updater(None).build()
pending_logins: dict[str, dict] = {}


def init_db() -> None:
    with get_db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id %s,
                chat_id BIGINT NOT NULL,
                chat_title TEXT NOT NULL,
                user_name TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""" % ("BIGSERIAL PRIMARY KEY" if using_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT")
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_id, created_at)"
        )
    init_user_table()


def save_message(chat_id: int, chat_title: str, user_name: str, text: str) -> None:
    with get_db() as connection:
        connection.execute(
            "INSERT INTO messages (chat_id, chat_title, user_name, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, chat_title, user_name, text, datetime.now(timezone.utc).isoformat()),
        )


def read_messages(chat_id: int, hours: int) -> list[Any]:
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
        "Halo, saya Hermes, bot AI untuk membantu Anda mengikuti percakapan Telegram.\n\n"
        "Yang bisa saya lakukan:\n"
        "- Memilih grup dari akun Telegram Anda tanpa memasukkan bot ke grup.\n"
        "- Merangkum berita, keputusan, tugas, perubahan, dan pertanyaan penting.\n"
        "- Menerjemahkan teks atau ringkasan ke berbagai bahasa.\n\n"
        "Cara mulai:\n"
        "1. /connect - hubungkan akun Telegram lewat halaman HTTPS.\n"
        "2. /grup - tampilkan daftar grup Anda.\n"
        "3. Tekan grup yang ingin diringkas.\n"
        "4. /summary_group - buat ringkasan grup terpilih.\n\n"
        "Perintah lain:\n"
        "/summary_group en - ringkasan dalam Inggris\n"
        "/translate ja teks - terjemahkan teks\n"
        "/translate_summary en - terjemahkan ringkasan terakhir\n"
        "/help - lihat panduan lengkap\n\n"
        "Saya hanya mengakses grup setelah Anda menghubungkan akun dan memilihnya."
    )


async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or update.effective_chat.type != ChatType.PRIVATE:
        return
    token = secrets.token_urlsafe(32)
    pending_logins[token] = {"user_id": update.effective_user.id, "created_at": time.time()}
    await update.effective_message.reply_text(
        f"Buka link aman ini untuk menghubungkan akun Telegram Anda:\n{WEBHOOK_URL}/login/{token}\n\n"
        "Link berlaku 10 menit. Nomor, kode login, dan password 2FA hanya dimasukkan di halaman tersebut, bukan ke chat bot."
    )


def get_pending_login(token: str) -> dict:
    login = pending_logins.get(token)
    if not login or time.time() - login["created_at"] > 600:
        pending_logins.pop(token, None)
        raise HTTPException(status_code=410, detail="Link login sudah kedaluwarsa")
    return login


async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or update.effective_chat.type != ChatType.PRIVATE:
        return
    state = load_user_state(update.effective_user.id)
    previous_message_id = state["group_message_id"] if state else None
    if previous_message_id:
        try:
            await context.bot.delete_message(update.effective_chat.id, previous_message_id)
        except Exception:
            logger.debug("Previous group list could not be deleted", exc_info=True)
    try:
        await delete_legacy_group_lists(update.effective_user.id, TELEGRAM_BOT_USERNAME)
        groups = await list_groups(update.effective_user.id)
    except Exception as error:
        await update.effective_message.reply_text(str(error))
        return
    if not groups:
        await update.effective_message.reply_text("Tidak ada grup atau channel yang bisa dibaca.")
        return
    context.user_data["groups"] = {str(group["id"]): group["name"] for group in groups}
    buttons = [
        [InlineKeyboardButton(str(group["name"])[:55], callback_data=f"group:{group['id']}")]
        for group in groups[:40]
    ]
    group_message = await update.effective_message.reply_text(
        "Pilih grup yang ingin diringkas:", reply_markup=InlineKeyboardMarkup(buttons)
    )
    save_user_state(update.effective_user.id, group_message_id=group_message.message_id)


async def delete_replied_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    if not message.reply_to_message:
        await message.reply_text("Reply pesan bot yang ingin dihapus, lalu kirim /hapus.")
        return
    try:
        await context.bot.delete_message(
            chat_id=message.chat_id,
            message_id=message.reply_to_message.message_id,
        )
        await message.reply_text("Pesan lama sudah dihapus.")
    except Exception:
        logger.exception("Could not delete replied message")
        await message.reply_text("Pesan itu tidak bisa dihapus oleh bot. Hapus manual dari Telegram.")


async def select_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_id = query.data.split(":", 1)[1]
    group_name = context.user_data.get("groups", {}).get(group_id, "Grup terpilih")
    context.user_data["selected_group_id"] = int(group_id)
    context.user_data["selected_group_name"] = group_name
    save_user_state(
        update.effective_user.id,
        selected_group_id=int(group_id),
        selected_group_name=group_name,
    )
    await query.edit_message_text(
        f"Grup dipilih: {group_name}\nKirim /summary_group untuk merangkum 200 pesan terakhir."
    )


async def group_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or update.effective_chat.type != ChatType.PRIVATE:
        return
    group_id = context.user_data.get("selected_group_id")
    if not group_id:
        state = load_user_state(update.effective_user.id)
        group_id = state["selected_group_id"] if state else None
    if not group_id:
        await update.effective_message.reply_text("Kirim /grup dulu, lalu pilih grup.")
        return
    target_language = LANGUAGE_ALIASES.get(context.args[0].lower(), context.args[0]) if context.args else "Bahasa Indonesia"
    await update.effective_message.reply_text("Sedang mengambil chat dan membuat ringkasan...")
    try:
        messages = await read_group_messages(update.effective_user.id, group_id)
        transcript = "\n".join(
            f"[{message['created_at']}] {message['name']}: {message['text']}" for message in messages
        )
        summary = await summarize_with_hermes(transcript, 24, target_language)
    except Exception as error:
        logger.exception("Selected group summary failed")
        await update.effective_message.reply_text(
            f"Gagal mengambil atau merangkum grup ({type(error).__name__}). "
            "Coba lagi setelah deployment terbaru aktif."
        )
        return
    context.user_data["last_summary"] = summary
    for part in split_message(summary):
        await update.effective_message.reply_text(part)


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
    target_language = "Bahasa Indonesia"
    for argument in context.args:
        if argument.isdigit():
            hours = max(1, min(int(argument), 168))
        else:
            target_language = LANGUAGE_ALIASES.get(argument.lower(), argument)
    messages = read_messages(update.effective_chat.id, hours)
    if not messages:
        await update.effective_message.reply_text(f"Belum ada pesan dalam {hours} jam terakhir.")
        return

    transcript = "\n".join(
        f"[{row['created_at']}] {row['user_name']}: {row['text']}" for row in messages
    )
    await update.effective_message.reply_text("Sedang membuat ringkasan...")
    try:
        summary = await summarize_with_hermes(transcript, hours, target_language)
    except Exception:
        logger.exception("Hermes request failed")
        await update.effective_message.reply_text(
            "Gagal menghubungi Hermes. Coba lagi beberapa saat lagi."
        )
        return

    context.user_data["last_summary"] = summary
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
        "Pakai akun Telegram pribadi:\n"
        "/connect - tampilkan QR untuk menghubungkan akun\n"
        "/grup - tampilkan grup dan channel Anda\n"
        "/hapus - hapus pesan bot dengan cara reply pesan tersebut\n"
        "Pilih tombol grup, lalu /summary_group\n"
        "/summary_group en - ringkas grup terpilih dalam Inggris\n"
        "Buka link login HTTPS yang dikirim bot. OTP dan password hanya dimasukkan di halaman itu.\n\n"
        "Ringkasan:\n"
        "/summary - ringkas pesan 24 jam terakhir\n"
        "/summary 6 - ringkas pesan 6 jam terakhir\n"
        "/summary en - ringkas langsung dalam Inggris\n"
        "/digest - alias /summary\n\n"
        "Translate:\n"
        "/translate en teks\n"
        "/translate Japanese teks\n"
        "/translate_summary en - terjemahkan ringkasan terakhir\n"
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
        "Mode grup lama:\n"
        "Bot mengumpulkan teks setelah ditambahkan. Hanya admin yang dapat meminta summary.\n\n"
        "Catatan: /grup memakai akun Telegram Anda, jadi bot tidak perlu masuk ke grup."
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


async def translate_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    if not context.args:
        await message.reply_text("Format: /translate_summary en")
        return
    target_key = context.args[0].lower()
    target = LANGUAGE_ALIASES.get(target_key, context.args[0])
    summary = context.user_data.get("last_summary")
    if not summary:
        await message.reply_text("Belum ada ringkasan. Jalankan /summary terlebih dahulu.")
        return
    try:
        translation = await translate_with_hermes(summary, target)
    except Exception:
        logger.exception("Hermes summary translation request failed")
        await message.reply_text("Gagal menerjemahkan ringkasan. Coba lagi nanti.")
        return
    for part in split_message(translation):
        await message.reply_text(part)


async def summarize_with_hermes(
    transcript: str, hours: int, target_language: str = "Bahasa Indonesia"
) -> str:
    prompt = (
        f"Analisis percakapan Telegram berikut untuk {hours} jam terakhir. "
        "Abaikan sapaan, basa-basi, candaan, pengulangan, dan pesan tanpa informasi baru. "
        "Pertahankan hanya berita, fakta, perubahan penting, keputusan, tenggat, tugas, risiko, "
        "pertanyaan yang belum terjawab, atau informasi yang bisa membuat pembaca ketinggalan konteks. "
        "Jika tidak ada hal penting, katakan persis: Tidak ada informasi penting. "
        f"Tulis dalam {target_language} dengan format rapi berikut:\n"
        "RINGKASAN PENTING\n- poin singkat\n\n"
        "KEPUTUSAN DAN TUGAS\n- siapa melakukan apa dan kapan\n\n"
        "BERITA ATAU PERUBAHAN\n- fakta atau perubahan penting\n\n"
        "PERTANYAAN TERBUKA\n- hal yang belum jelas\n"
        "Jika bagian tidak ada, tulis '- Tidak ada'. Jangan mengarang.\n\n" + transcript
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
telegram_app.add_handler(CommandHandler("connect", connect_start))
telegram_app.add_handler(CommandHandler("grup", groups_command))
telegram_app.add_handler(CommandHandler("groups", groups_command))
telegram_app.add_handler(CommandHandler("hapus", delete_replied_message))
telegram_app.add_handler(CommandHandler("summary_group", group_summary_command))
telegram_app.add_handler(CallbackQueryHandler(select_group, pattern=r"^group:"))
telegram_app.add_handler(CommandHandler("summary", summary_command))
telegram_app.add_handler(CommandHandler("digest", summary_command))
telegram_app.add_handler(CommandHandler("translate", translate_command))
telegram_app.add_handler(CommandHandler("translate_summary", translate_summary_command))
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


def login_page(token: str, message: str = "", step: str = "phone") -> str:
    field = "phone" if step == "phone" else "code" if step == "code" else "password"
    label = "Nomor Telegram (+kode negara)" if field == "phone" else "Kode login Telegram" if field == "code" else "Password 2FA Telegram"
    input_type = "password" if field == "password" else "text"
    title = "Hubungkan Telegram"
    safe_message = html.escape(message)
    safe_token = html.escape(token, quote=True)
    return f"""<!doctype html>
<html lang="id"><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>{title}</title>
<style>
:root{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#f7f5ff;background:#0d0b16}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 15% 10%,#39235f 0,#0d0b16 42%),#0d0b16}}
.shell{{width:min(100%,480px)}}.brand{{display:flex;align-items:center;gap:10px;margin:0 0 18px 4px;font-weight:700;letter-spacing:.2px}}.mark{{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:#8b5cf6;color:white;font-size:18px}}
main{{padding:30px;border:1px solid #3a3150;border-radius:20px;background:rgba(26,21,39,.92);box-shadow:0 24px 70px rgba(0,0,0,.38)}}h1{{margin:0;font-size:28px;line-height:1.1}}.step{{margin:10px 0 0;color:#a9a1bb;font-size:14px}}.note{{margin:24px 0;color:#c5bdd5;line-height:1.55;font-size:14px}}label{{display:block;margin-top:22px;font-size:14px;font-weight:650}}input{{width:100%;margin-top:9px;padding:14px 15px;border:1px solid #51466c;border-radius:11px;background:#120f1d;color:#fff;font-size:16px;outline:none}}input:focus{{border-color:#a78bfa;box-shadow:0 0 0 3px rgba(167,139,250,.18)}}button{{width:100%;margin-top:18px;padding:14px;border:0;border-radius:11px;background:#8b5cf6;color:#fff;font-size:15px;font-weight:700;cursor:pointer}}button:hover{{background:#9b75f7}}.error{{margin:18px 0 0;padding:12px 14px;border:1px solid #7f3847;border-radius:10px;background:#351923;color:#ffb4be;font-size:14px;line-height:1.45}}
.privacy{{margin:18px 2px 0;color:#827a96;font-size:12px;line-height:1.5;text-align:center}}
</style></head><body><div class="shell"><div class="brand"><span class="mark">H</span><span>Hermes Telegram</span></div><main><h1>{title}</h1><p class="step">Langkah verifikasi akun Anda</p><p class="note">Data ini hanya dipakai untuk proses login sementara. Jangan bagikan link ini kepada siapa pun.</p>{f'<p class="error">{safe_message}</p>' if message else ''}<form method="post" action="/login/{safe_token}/{field}"><label for="value">{label}</label><input id="value" name="value" type="{input_type}" autocomplete="off" required autofocus><button type="submit">Lanjutkan</button></form></main><p class="privacy">Koneksi terenkripsi melalui HTTPS</p></div></body></html>"""


@api.get("/login/{token}", response_class=HTMLResponse)
async def login_form(token: str):
    get_pending_login(token)
    return login_page(token)


@api.post("/login/{token}/phone", response_class=HTMLResponse)
async def login_phone(token: str, value: str = Form(...)):
    login = get_pending_login(token)
    phone = value.strip()
    if not phone.startswith("+"):
        return login_page(token, "Nomor harus diawali + dan kode negara.")
    client = new_client()
    try:
        await client.connect()
        sent_code = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await client.disconnect()
        return login_page(token, "Nomor tidak valid. Gunakan format +628xxxxxxxxxx.")
    except PhoneNumberBannedError:
        await client.disconnect()
        return login_page(token, "Nomor Telegram ini diblokir Telegram.")
    except ApiIdInvalidError:
        await client.disconnect()
        logger.error("Telegram API ID or hash is invalid")
        return login_page(token, "TELEGRAM_API_ID atau TELEGRAM_API_HASH di Railway tidak valid.")
    except FloodWaitError as error:
        await client.disconnect()
        return login_page(token, f"Terlalu banyak percobaan. Tunggu {error.seconds} detik.")
    except Exception as error:
        await client.disconnect()
        logger.exception("Telegram web login code request failed")
        logger.error("Telegram login error type: %s", type(error).__name__)
        return login_page(token, f"Telegram menolak permintaan ({type(error).__name__}). Cek Railway Logs.")
    login.update(client=client, phone=phone, phone_code_hash=sent_code.phone_code_hash)
    return login_page(token, step="code")


@api.post("/login/{token}/code", response_class=HTMLResponse)
async def login_code(token: str, value: str = Form(...)):
    login = get_pending_login(token)
    client = login.get("client")
    if not client:
        return login_page(token, "Sesi login sudah kedaluwarsa.")
    try:
        await client.sign_in(phone=login["phone"], code=value.strip(), phone_code_hash=login["phone_code_hash"])
    except SessionPasswordNeededError:
        return login_page(token, step="password")
    except Exception:
        await client.disconnect()
        return login_page(token, "Kode salah atau sudah kedaluwarsa.", step="code")
    return await finish_web_login(token, login)


@api.post("/login/{token}/password", response_class=HTMLResponse)
async def login_password(token: str, value: str = Form(...)):
    login = get_pending_login(token)
    client = login.get("client")
    if not client:
        return login_page(token, "Sesi login sudah kedaluwarsa.")
    try:
        await client.sign_in(password=value)
    except Exception:
        await client.disconnect()
        return login_page(token, "Password 2FA salah.", step="password")
    return await finish_web_login(token, login)


async def finish_web_login(token: str, login: dict):
    client = login["client"]
    try:
        save_session(login["user_id"], client.session.save())
    except Exception as error:
        logger.exception("Could not save Telegram session")
        await client.disconnect()
        return HTMLResponse(
            login_page(
                token,
                f"Session gagal disimpan ({type(error).__name__}). Buka Railway Logs.",
            ),
            status_code=500,
        )
    finally:
        if not client.is_connected():
            pending_logins.pop(token, None)
        else:
            await client.disconnect()
    pending_logins.pop(token, None)
    try:
        await telegram_app.bot.send_message(login["user_id"], "Akun berhasil terhubung. Kirim /grup untuk memilih grup.")
    except Exception:
        logger.exception("Could not send Telegram login confirmation")
    return HTMLResponse(f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{{font-family:system-ui;max-width:520px;margin:80px auto;padding:24px;background:#0d0b16;color:#f7f5ff}}main{{padding:28px;border:1px solid #3a3150;border-radius:18px;background:#1a1527}}h1{{margin-top:0;color:#c4b5fd}}a{{display:block;text-align:center;margin-top:24px;padding:14px;border-radius:11px;background:#8b5cf6;color:white;text-decoration:none;font-weight:700}}</style><main><h1>Berhasil terhubung</h1><p>Akun Telegram sudah tersambung. Kembali ke Telegram dan kirim <b>/grup</b>.</p><a href="https://t.me/{TELEGRAM_BOT_USERNAME}">Kembali ke Telegram</a></main>""")


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
