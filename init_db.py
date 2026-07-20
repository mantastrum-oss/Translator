import os
import psycopg2
from pathlib import Path


def load_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    raise RuntimeError("Set DATABASE_URL first")


def init_db() -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    sql = schema_path.read_text(encoding="utf-8")
    conn = psycopg2.connect(load_database_url())
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("Database initialized successfully")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
