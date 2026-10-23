"""Make sure the dataset DB exists on the disk (spec §0.2 R12, A12).

Primary path: build torob.db locally and upload it to the host disk. Fallback (this module): if the file is missing and
DATASET_URL is set, download the official zip and build it in a background thread; /health says «building» meanwhile.
"""

import logging
import os
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

from torob import store

log = logging.getLogger("torob.boot")
_started = False
_lock = threading.Lock()
status = {"state": "idle", "detail": None}


def ensure_db_async() -> None:
    global _started
    with _lock:
        if _started or store.DB_PATH.exists() or not os.environ.get("DATASET_URL"):
            return
        _started = True
    threading.Thread(target=_build, name="torob-db-build", daemon=True).start()


def _build() -> None:
    from torob.etl.build_db import build   # duckdb is only needed on this path
    url = os.environ["DATASET_URL"]
    status.update(state="downloading", detail=None)
    work = Path(tempfile.mkdtemp(prefix="torob-ds-", dir=store.DB_PATH.parent))
    try:
        zpath = work / "dataset.zip"
        with urllib.request.urlopen(url, timeout=600) as r, zpath.open("wb") as f:   # official dataset URL from env
            shutil.copyfileobj(r, f)
        status.update(state="unpacking")
        with zipfile.ZipFile(zpath) as z:
            for name in ("shops.csv", "products.csv", "offers.csv"):
                z.extract(name, work)
        status.update(state="building")
        build(work, store.DB_PATH)
        status.update(state="ready")
        log.info("dataset DB built at %s", store.DB_PATH)
    except Exception as e:   # leave the app up and say so on /health
        status.update(state="failed", detail=type(e).__name__)
        log.exception("dataset DB build failed")
    finally:
        shutil.rmtree(work, ignore_errors=True)
