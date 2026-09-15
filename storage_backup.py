"""RelaxDev Storage backup/restore for the Stage I SQLite research database."""
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

import requests

STORAGE_API_KEY = os.environ.get("STORAGE_API_KEY", "").strip()
STORAGE_API_BASE = os.environ.get("STORAGE_API_BASE", "https://relaxdev.ru/api/v1/storage").rstrip("/")
STORAGE_BACKUP_PATH = os.environ.get("STORAGE_BACKUP_PATH", "magnet-backups")
STORAGE_BACKUP_INTERVAL_SECONDS = int(os.environ.get("STORAGE_BACKUP_INTERVAL_SECONDS", "3600"))
STORAGE_BACKUP_KEEP = int(os.environ.get("STORAGE_BACKUP_KEEP", "24"))

_last_backup_ts = 0.0


def enabled():
    return bool(STORAGE_API_KEY)


def _headers():
    return {"Authorization": f"Bearer {STORAGE_API_KEY}"}


def _sqlite_snapshot(db_path):
    src = sqlite3.connect(db_path, timeout=30)
    fd, tmp = tempfile.mkstemp(prefix="magnet_research_", suffix=".sqlite3")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp, timeout=30)
        src.backup(dst)
        dst.execute("PRAGMA integrity_check")
        dst.close()
        return tmp
    finally:
        src.close()


def upload_backup(db_path):
    if not enabled() or not os.path.exists(db_path):
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    filename = f"magnet_research_{stamp}.sqlite3"
    tmp = _sqlite_snapshot(db_path)
    try:
        with open(tmp, "rb") as fh:
            r = requests.post(
                f"{STORAGE_API_BASE}/upload",
                headers=_headers(),
                files={"file": (filename, fh, "application/x-sqlite3")},
                data={"path": STORAGE_BACKUP_PATH},
                timeout=120,
            )
        r.raise_for_status()
        payload = r.json()
        if not payload.get("success", True):
            raise RuntimeError(f"Storage upload failed: {payload}")
        return payload
    finally:
        try: os.remove(tmp)
        except OSError: pass


def list_backups():
    if not enabled():
        return []
    r = requests.get(
        f"{STORAGE_API_BASE}/files",
        headers=_headers(),
        params={"path": STORAGE_BACKUP_PATH},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("files", [])


def cleanup_old_backups():
    if not enabled() or STORAGE_BACKUP_KEEP <= 0:
        return 0
    files = [x for x in list_backups() if re.search(r"magnet_research_\d{8}_\d{6}\.sqlite3$", str(x.get("path", "")))]
    files.sort(key=lambda x: str(x.get("lastModified", "")), reverse=True)
    removed = 0
    for item in files[STORAGE_BACKUP_KEEP:]:
        path = item.get("path")
        if not path:
            continue
        r = requests.delete(
            f"{STORAGE_API_BASE}/files",
            headers=_headers(),
            params={"path": path},
            timeout=30,
        )
        r.raise_for_status()
        removed += 1
    return removed


def maybe_backup(db_path, force=False):
    global _last_backup_ts
    if not enabled() or not os.path.exists(db_path):
        return None
    now = time.time()
    if not force and now - _last_backup_ts < STORAGE_BACKUP_INTERVAL_SECONDS:
        return None
    payload = upload_backup(db_path)
    _last_backup_ts = now
    try:
        cleanup_old_backups()
    except Exception as exc:
        print(f"[Storage] cleanup failed: {type(exc).__name__}: {exc}")
    print(f"[Storage] SQLite backup uploaded: {db_path}")
    return payload


def restore_latest(db_path):
    """Restore only when the local DB is absent/empty; never overwrite a populated DB."""
    if not enabled() or os.path.exists(db_path) and os.path.getsize(db_path) > 1024:
        return False
    files = list_backups()
    files = [x for x in files if re.search(r"magnet_research_\d{8}_\d{6}\.sqlite3$", str(x.get("path", ""))) and x.get("url")]
    if not files:
        return False
    files.sort(key=lambda x: str(x.get("lastModified", "")), reverse=True)
    src = files[0]
    r = requests.get(src["url"], timeout=120)
    r.raise_for_status()
    fd, tmp = tempfile.mkstemp(prefix="restore_", suffix=".sqlite3")
    os.close(fd)
    try:
        Path(tmp).write_bytes(r.content)
        conn = sqlite3.connect(tmp)
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.close()
        if not ok:
            raise RuntimeError("downloaded SQLite backup failed integrity_check")
        os.replace(tmp, db_path)
        print(f"[Storage] restored SQLite from {src.get('path')}")
        return True
    finally:
        try: os.remove(tmp)
        except OSError: pass
