"""Read-only access to the dataset DB (spec §0.2 R12). Sessions and logs live in a separate app.db (S1)."""

import math
import os
import sqlite3
import threading
from pathlib import Path

from core.text import normalize_fa

DB_PATH = Path(os.environ.get("TOROB_DB", "data/torob.db"))


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


_shared: sqlite3.Connection | None = None
_shared_sig: tuple[int, int] | None = None
_shared_lock = threading.Lock()


def get() -> sqlite3.Connection:
    """The one shared dataset connection, reopened whenever the DB file changes (a finished upload replaces a
    partial one). Everything (app routes and agents) uses this, so nothing reads a stale file."""
    global _shared, _shared_sig
    with _shared_lock:
        sig = file_signature()
        if sig is None:
            raise FileNotFoundError(DB_PATH)
        if _shared is None or sig != _shared_sig:
            if _shared is not None:
                _shared.close()
            _shared, _shared_sig = connect(), sig
        return _shared


def reset() -> None:
    global _shared, _shared_sig
    with _shared_lock:
        if _shared is not None:
            _shared.close()
        _shared = _shared_sig = None


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
