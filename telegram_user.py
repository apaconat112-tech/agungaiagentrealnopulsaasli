import os
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from telethon import TelegramClient
from telethon.sessions import StringSession

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/messages.db")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")


def _connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_user_table() -> None:
    with _connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_accounts (
                user_id INTEGER PRIMARY KEY,
                session_token TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def _cipher() -> Fernet:
    if not SESSION_ENCRYPTION_KEY:
        raise RuntimeError("SESSION_ENCRYPTION_KEY belum diatur di Railway Variables")
    return Fernet(SESSION_ENCRYPTION_KEY.encode())


def save_session(user_id: int, session_string: str) -> None:
    encrypted = _cipher().encrypt(session_string.encode()).decode()
    with _connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO telegram_accounts (user_id, session_token, created_at) VALUES (?, ?, ?)",
            (user_id, encrypted, datetime.now(timezone.utc).isoformat()),
        )


def load_session(user_id: int) -> str | None:
    with _connection() as connection:
        row = connection.execute(
            "SELECT session_token FROM telegram_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    return _cipher().decrypt(row["session_token"].encode()).decode()


def new_client(session_string: str = "") -> TelegramClient:
    if not API_ID or not API_HASH:
        raise RuntimeError("TELEGRAM_API_ID dan TELEGRAM_API_HASH belum diatur di Railway Variables")
    return TelegramClient(StringSession(session_string), API_ID, API_HASH)


async def list_groups(user_id: int) -> list[dict[str, str | int]]:
    session = load_session(user_id)
    if not session:
        raise RuntimeError("Akun Telegram belum terhubung. Gunakan /connect terlebih dahulu.")
    client = new_client(session)
    groups = []
    async with client:
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                groups.append(
                    {
                        "id": dialog.id,
                        "name": dialog.name or "Tanpa nama",
                        "kind": "channel" if dialog.is_channel and not dialog.is_group else "group",
                    }
                )
    return groups


async def read_group_messages(user_id: int, group_id: int, limit: int = 200) -> list[dict[str, str]]:
    session = load_session(user_id)
    if not session:
        raise RuntimeError("Akun Telegram belum terhubung. Gunakan /connect terlebih dahulu.")
    client = new_client(session)
    messages = []
    async with client:
        entity = await client.get_entity(group_id)
        async for message in client.iter_messages(entity, limit=limit, reverse=True):
            if message.message:
                sender = await message.get_sender()
                name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "Pengguna"
                messages.append(
                    {
                        "name": name,
                        "text": message.message,
                        "created_at": message.date.isoformat(),
                    }
                )
    return messages
