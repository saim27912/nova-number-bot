import os
import sqlite3
from urllib.parse import urlparse, unquote
import pg8000

tables = {
    "users": ["id", "name", "balance", "premium_until", "banned", "otp_count", "joined"],
    "numbers": ["id", "user_id", "number", "rid", "service", "country", "otp", "status", "created"],
    "withdrawals": ["id", "user_id", "amount", "method", "status", "created"],
    "settings": ["key", "value"],
    "stock_groups": ["id", "service", "country"],
    "stock": ["id", "gid", "number", "status", "added"],
    "user_state": ["user_id", "service", "rid"]
}

schema = {
    "users": """id BIGINT PRIMARY KEY, name TEXT, balance DOUBLE PRECISION DEFAULT 0,
    premium_until BIGINT DEFAULT 0, banned BIGINT DEFAULT 0, otp_count BIGINT DEFAULT 0, joined BIGINT""",
    "numbers": """id BIGSERIAL PRIMARY KEY, user_id BIGINT, number TEXT, rid TEXT,
    service TEXT, country TEXT, otp TEXT, status TEXT, created BIGINT""",
    "withdrawals": """id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount DOUBLE PRECISION,
    method TEXT, status TEXT DEFAULT 'pending', created BIGINT""",
    "settings": """key TEXT PRIMARY KEY, value TEXT""",
    "stock_groups": """id BIGSERIAL PRIMARY KEY, service TEXT, country TEXT""",
    "stock": """id BIGSERIAL PRIMARY KEY, gid BIGINT, number TEXT, status TEXT DEFAULT 'free', added BIGINT""",
    "user_state": """user_id BIGINT PRIMARY KEY, service TEXT, rid TEXT"""
}

url = urlparse(os.environ["DATABASE_URL"])
conn = pg8000.connect(
    user=unquote(url.username),
    password=unquote(url.password),
    host=url.hostname,
    port=url.port or 5432,
    database=url.path.lstrip("/"),
    ssl_context=True,
    timeout=20
)

src = sqlite3.connect("bot.db")
src.row_factory = sqlite3.Row
cur = conn.cursor()

try:
    for table, columns in tables.items():
        cur.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({schema[table]})')

    for table, columns in tables.items():
        rows = src.execute(
            f'SELECT {",".join(chr(34)+c+chr(34) for c in columns)} FROM "{table}"'
        ).fetchall()

        names = ",".join(f'"{c}"' for c in columns)
        placeholders = ",".join(["%s"] * len(columns))
        sql = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'

        for row in rows:
            cur.execute(sql, tuple(row))

        print(f"{table}: {len(rows)} source records processed")

    for table in ["numbers", "withdrawals", "stock_groups", "stock"]:
        cur.execute(f'SELECT MAX(id) FROM "{table}"')
        maximum = cur.fetchone()[0]
        if maximum is not None:
            cur.execute(
                "SELECT setval(pg_get_serial_sequence(%s, 'id'), %s, true)",
                (table, maximum)
            )

    conn.commit()
    print("\nMIGRATION COMPLETED")

    print("\nPostgreSQL record counts:")
    for table in tables:
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        print(f"{table}: {cur.fetchone()[0]}")

except Exception:
    conn.rollback()
    raise
finally:
    src.close()
    cur.close()
    conn.close()
