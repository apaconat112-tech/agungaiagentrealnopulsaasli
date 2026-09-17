import os
import sqlite3
from typing import Any

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/messages.db")


class DatabaseConnection:
    def __init__(self):
        if DATABASE_URL:
            self._connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)
            self.is_postgres = True
        else:
            os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
            self._connection = sqlite3.connect(DATABASE_PATH)
            self._connection.row_factory = sqlite3.Row
            self.is_postgres = False

    def execute(self, query: str, parameters: tuple[Any, ...] = ()):
        if self.is_postgres:
            query = query.replace("?", "%s")
        return self._connection.execute(query, parameters)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)


def get_db() -> DatabaseConnection:
    return DatabaseConnection()


def using_postgres() -> bool:
    return bool(DATABASE_URL)
