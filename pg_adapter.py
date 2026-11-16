import os
import threading
from urllib.parse import urlparse, unquote
import pg8000.dbapi


class Row(dict):
    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.columns = [c[0] for c in (cursor.description or [])]

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(self.columns, row) if row is not None else None

    def fetchall(self):
        return [Row(self.columns, r) for r in self.cursor.fetchall()]


class Database:
    def __init__(self):
        url = os.environ["DATABASE_URL"]
        u = urlparse(url)
        self.lock = threading.RLock()
        self.conn = pg8000.dbapi.connect(
            user=unquote(u.username),
            password=unquote(u.password),
            host=u.hostname,
            port=u.port or 5432,
            database=u.path.lstrip("/"),
            ssl_context=True,
            timeout=20,
        )

    def execute(self, sql, args=()):
        sql = sql.replace("?", "%s")
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(sql, args)
            return Cursor(cur)

    def commit(self):
        with self.lock:
            self.conn.commit()

    def executescript(self, script):
        with self.lock:
            cur = self.conn.cursor()
            for statement in script.split(";"):
                if statement.strip():
                    cur.execute(statement)
            self.conn.commit()


db = Database()
print("PostgreSQL adapter connected")
