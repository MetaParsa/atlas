"""Read-only access to the dataset DB (spec §0.2 R12). Sessions and logs live in a separate app.db (S1)."""

import math
import os
import sqlite3
import threading
from pathlib import Path

from core.text import normalize_fa

DB_PATH = Path(os.environ.get("TOROB_DB", "data/torob.db"))


def sells(shop_id: str, product_id: str) -> bool:
    """The shop registered this product (any price): it may be called about it."""
    return get().execute("SELECT 1 FROM offers WHERE shop_id = ? AND product_id = ? LIMIT 1",
                         (shop_id, product_id)).fetchone() is not None


def _uri() -> str:
    # mode=ro (not immutable): SQLite still notices if the file is replaced by a new upload.
    return f"file:{DB_PATH.resolve().as_posix()}?mode=ro"


def file_signature() -> tuple[int, int] | None:
    """(size, mtime) of the DB file, used to reopen after an upload; None if it isn't there yet."""
    try:
        st = DB_PATH.stat()
        return st.st_size, st.st_mtime_ns
    except FileNotFoundError:
        return None


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(_uri(), uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.create_function("ln1p", 1, math.log1p, deterministic=True)  # bundled SQLite may lack math functions
    return con


_local = threading.local()   # one connection per thread: a shared one deadlocked under concurrent searches
_epoch = 0                   # bumped by reset(): every thread reopens on its next call


def get() -> sqlite3.Connection:
    """This thread's dataset connection, reopened whenever the DB file changes (a finished upload replaces a partial
    one) or after reset(). One per thread: SQLite calls back into Python for `ln1p`, and two threads on one connection
    can block each other forever (seen live with three searches at once)."""
    sig = file_signature()
    if sig is None:
        raise FileNotFoundError(DB_PATH)
    con, con_sig, con_epoch = getattr(_local, "con", None), getattr(_local, "sig", None), getattr(_local, "epoch", -1)
    if con is None or con_sig != sig or con_epoch != _epoch:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        con = connect()
        _local.con, _local.sig, _local.epoch = con, sig, _epoch
    return con


def reset() -> None:
    global _epoch
    _epoch += 1


def meta(con: sqlite3.Connection) -> dict[str, str]:
    return dict(con.execute("SELECT key, value FROM meta").fetchall())


def areas(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT area, lat, lon, shops, shops_with_offers FROM areas ORDER BY shops DESC").fetchall()
    return [dict(r) for r in rows]


def fts_query(text: str) -> str:
    """User text -> FTS5 OR-query of quoted normalised tokens (quotes stop FTS syntax injection)."""
    tokens = [t for t in normalize_fa(text).replace('"', " ").split() if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in tokens)


def search_products(con: sqlite3.Connection, text: str, limit: int = 20, category: str | None = None) -> list[dict]:
    """BM25 over title/brand/category, boosted by how many shops list the product (spec R12)."""
    q = fts_query(text)
    if not q:
        return []
    sql = """
      SELECT p.product_id, p.title, p.brand, p.category, p.sellers_total, p.price_min, p.price_median, p.price_max,
             bm25(products_fts, 4.0, 2.0, 1.0) - 0.8 * ln1p(p.sellers_total) AS score
      FROM products_fts JOIN products p ON p.rid = products_fts.rowid
      WHERE products_fts MATCH ? AND p.redacted = 0 {cat}
      ORDER BY score LIMIT ?"""
    args: list = [q]
    cat = ""
    if category:
        cat, args = "AND p.category = ?", [q, category]
    return [dict(r) for r in con.execute(sql.format(cat=cat), (*args, limit)).fetchall()]
