"""SQLite log of everything the server was asked to say, and the audio to
replay it.

A row is written once, when the utterance reaches a terminal state (spoke,
failed, dropped, …) — never updated. In-flight work is visible from the queue
instead, so there is no window where a row is half-true and no update races
between the player thread and HTTP handlers.

Audio for replay is usually just the cache entry, referenced by path. When the
cache wouldn't have kept it (text too long, caching off) a copy goes to a clips
directory under the same retention, so the dashboard's play button works for
every row rather than mysteriously only some.
"""

import logging
import os
import secrets
import sqlite3
import threading
import time

import config

log = logging.getLogger("history")

SCHEMA = """
CREATE TABLE IF NOT EXISTS utterances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   REAL    NOT NULL,
    source       TEXT,
    client       TEXT,
    text         TEXT    NOT NULL,
    engine       TEXT,
    engine_used  TEXT,
    voice        TEXT,
    speed        REAL,
    lang         TEXT,
    priority     INTEGER,
    sink         TEXT,
    volume       INTEGER,
    status       TEXT    NOT NULL,
    error        TEXT,
    queue_ms     INTEGER,
    synth_ms     INTEGER,
    play_ms      INTEGER,
    audio_ms     INTEGER,
    cache_hit    INTEGER,
    clip_path    TEXT
);
CREATE INDEX IF NOT EXISTS idx_utterances_created ON utterances (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_utterances_status  ON utterances (status);
"""

# Terminal states a row can carry. Kept here (not in config) because they are
# part of the API contract the dashboard filters on.
STATUSES = (
    "spoke",       # synthesized and played to completion
    "failed",      # synthesis or playback error
    "interrupted", # cut off by a higher-priority utterance
    "dropped",     # refused by policy (quiet hours, full queue)
    "expired",     # sat in the queue past its TTL
    "cancelled",   # removed by an operator
    "muted",       # accepted while muted, so never audible
)


class History:
    def __init__(self):
        self.enabled = config.HISTORY_ENABLED
        self._lock = threading.Lock()
        self._conn = None
        if not self.enabled:
            return
        try:
            os.makedirs(os.path.dirname(config.HISTORY_DB) or ".", exist_ok=True)
            # One connection shared under a lock. Writes are single-row and
            # sub-millisecond, and the alternative (a connection per thread)
            # buys nothing when the player thread does nearly all the writing.
            self._conn = sqlite3.connect(
                config.HISTORY_DB, check_same_thread=False, timeout=10
            )
            self._conn.row_factory = sqlite3.Row
            # WAL so a dashboard read never blocks the player thread's write.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        except (OSError, sqlite3.Error) as e:
            log.warning("cannot open history db %s (%s); history disabled",
                        config.HISTORY_DB, e)
            self.enabled = False
            self._conn = None
        if self.enabled and config.HISTORY_KEEP_AUDIO:
            try:
                os.makedirs(config.HISTORY_AUDIO_DIR, exist_ok=True)
            except OSError as e:
                log.warning("cannot create clips dir (%s); replay limited to cached audio", e)

    # ---- writing ------------------------------------------------------

    def store_clip(self, wav_bytes):
        """Keep a copy of audio that the cache didn't. Returns a path or None.
        Named randomly rather than by row id because the path has to exist
        before the row it belongs to."""
        if not (self.enabled and config.HISTORY_KEEP_AUDIO and wav_bytes):
            return None
        path = os.path.join(config.HISTORY_AUDIO_DIR, f"{secrets.token_hex(8)}.wav")
        try:
            with open(path, "wb") as fh:
                fh.write(wav_bytes)
            return path
        except OSError as e:
            log.debug("clip write failed: %s", e)
            return None

    def record(self, **fields):
        """Insert one terminal-state row. Never raises: losing a history row is
        not a reason to fail an utterance that already played."""
        if not self.enabled:
            return None
        fields.setdefault("created_at", time.time())
        columns = [
            "created_at", "source", "client", "text", "engine", "engine_used",
            "voice", "speed", "lang", "priority", "sink", "volume",
            "status", "error", "queue_ms", "synth_ms", "play_ms", "audio_ms",
            "cache_hit", "clip_path",
        ]
        values = [fields.get(c) for c in columns]
        placeholders = ", ".join("?" * len(columns))
        try:
            with self._lock:
                cur = self._conn.execute(
                    f"INSERT INTO utterances ({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
                self._conn.commit()
                return cur.lastrowid
        except sqlite3.Error as e:
            log.warning("history insert failed: %s", e)
            return None

    # ---- reading ------------------------------------------------------

    def query(self, limit=50, offset=0, status=None, search=None, source=None):
        if not self.enabled:
            return []
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if source:
            where.append("source = ?")
            params.append(source)
        if search:
            where.append("text LIKE ?")
            params.append(f"%{search}%")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            f"SELECT * FROM utterances {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        params.extend([max(1, min(500, int(limit))), max(0, int(offset))])
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            log.warning("history query failed: %s", e)
            return []
        return [self._row_to_dict(r) for r in rows]

    def get(self, row_id, keep_path=False):
        if not self.enabled:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM utterances WHERE id = ?", (int(row_id),)
                ).fetchone()
        except (sqlite3.Error, ValueError):
            return None
        return self._row_to_dict(row, keep_path=keep_path) if row else None

    def clip_bytes(self, row_id):
        """Audio for one row, for replay. Returns None when the clip has been
        evicted — the dashboard shows the row without a play button rather than
        pretending the audio is still there."""
        row = self.get(row_id, keep_path=True)
        if not row or not row.get("clip_path"):
            return None
        try:
            with open(row["clip_path"], "rb") as fh:
                return fh.read()
        except OSError:
            return None

    @staticmethod
    def _row_to_dict(row, keep_path=False):
        item = dict(row)
        item["cache_hit"] = bool(item.get("cache_hit"))
        clip_path = item.get("clip_path")
        # `has_audio` is what the dashboard needs; the path itself is filesystem
        # layout the browser has no business seeing, and the API serves audio by
        # row id instead. Internal callers ask for it explicitly.
        item["has_audio"] = bool(clip_path) and os.path.exists(clip_path)
        if not keep_path:
            item.pop("clip_path", None)
        return item

    def stats(self):
        if not self.enabled:
            return {"enabled": False}
        try:
            with self._lock:
                total = self._conn.execute("SELECT COUNT(*) FROM utterances").fetchone()[0]
                by_status = {
                    r[0]: r[1]
                    for r in self._conn.execute(
                        "SELECT status, COUNT(*) FROM utterances GROUP BY status"
                    ).fetchall()
                }
                spoken_today = self._conn.execute(
                    "SELECT COUNT(*) FROM utterances WHERE created_at > ?",
                    (time.time() - 86400,),
                ).fetchone()[0]
        except sqlite3.Error:
            return {"enabled": True, "error": "query failed"}
        return {
            "enabled": True,
            "rows": total,
            "by_status": by_status,
            "last_24h": spoken_today,
            "max_rows": config.HISTORY_MAX_ROWS,
        }

    # ---- retention ----------------------------------------------------

    def prune(self):
        """Trim rows past the age or count limit, delete the clips they owned,
        and sweep clips no row references (left behind by a crash between the
        file write and the insert)."""
        if not self.enabled:
            return 0
        removed = 0
        doomed_clips = []
        try:
            with self._lock:
                if config.HISTORY_MAX_AGE_DAYS > 0:
                    cutoff = time.time() - config.HISTORY_MAX_AGE_DAYS * 86400
                    rows = self._conn.execute(
                        "SELECT id, clip_path FROM utterances WHERE created_at < ?", (cutoff,)
                    ).fetchall()
                    doomed_clips += [r["clip_path"] for r in rows if r["clip_path"]]
                    cur = self._conn.execute(
                        "DELETE FROM utterances WHERE created_at < ?", (cutoff,)
                    )
                    removed += cur.rowcount
                if config.HISTORY_MAX_ROWS > 0:
                    rows = self._conn.execute(
                        "SELECT id, clip_path FROM utterances ORDER BY id DESC LIMIT -1 OFFSET ?",
                        (config.HISTORY_MAX_ROWS,),
                    ).fetchall()
                    if rows:
                        doomed_clips += [r["clip_path"] for r in rows if r["clip_path"]]
                        ids = [r["id"] for r in rows]
                        self._conn.executemany(
                            "DELETE FROM utterances WHERE id = ?", [(i,) for i in ids]
                        )
                        removed += len(ids)
                self._conn.commit()
        except sqlite3.Error as e:
            log.warning("history prune failed: %s", e)
            return removed

        for path in doomed_clips:
            # Only ever delete inside the clips dir: a row may point at a cache
            # entry instead, and the cache manages its own lifetime.
            if path and path.startswith(config.HISTORY_AUDIO_DIR):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self._sweep_clips()
        if removed:
            log.info("history pruned %d rows", removed)
        return removed

    def _sweep_clips(self):
        """Enforce the clips-directory size cap and drop unreferenced files."""
        if not (config.HISTORY_KEEP_AUDIO and os.path.isdir(config.HISTORY_AUDIO_DIR)):
            return
        try:
            with self._lock:
                referenced = {
                    r[0] for r in self._conn.execute(
                        "SELECT clip_path FROM utterances WHERE clip_path IS NOT NULL"
                    ).fetchall()
                }
        except sqlite3.Error:
            return
        entries = []
        try:
            for name in os.listdir(config.HISTORY_AUDIO_DIR):
                full = os.path.join(config.HISTORY_AUDIO_DIR, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if full not in referenced:
                    try:
                        os.unlink(full)
                    except OSError:
                        pass
                    continue
                entries.append((full, st.st_size, st.st_mtime))
        except OSError:
            return
        limit = config.HISTORY_AUDIO_MAX_MB * 1024 * 1024
        if limit <= 0:
            return
        total = sum(size for _f, size, _m in entries)
        for full, size, _mtime in sorted(entries, key=lambda e: e[2]):
            if total <= limit:
                break
            try:
                os.unlink(full)
                total -= size
            except OSError:
                pass


history = History()
