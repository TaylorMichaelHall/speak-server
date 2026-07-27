"""Content-addressed cache of synthesized audio, plus a small TTL cache of
engine voice lists.

Synthesis is the slow, expensive part: seconds of CPU locally, money remotely.
The phrases a notifier actually says repeat constantly ("build finished", "tests
passed", "deploy complete"), so caching turns those into a file read.

What's stored is the *final* WAV — after lead-silence padding — so a hit can be
handed straight to paplay as a path with nothing to decode or rewrite. That
means LEAD_SILENCE_MS is part of the key; changing it re-synthesizes once per
phrase, which is the right trade for making the hot path a bare file open.

Long text is deliberately not cached: a paragraph of build output is never asked
for twice, and it would evict the short phrases that are.
"""

import hashlib
import json
import logging
import os
import threading
import time

import config

log = logging.getLogger("cache")


def cache_key(engine, model, voice, speed, lang, text, lead_silence_ms):
    """Every input that changes the audio goes in, so a hit is byte-identical to
    what a fresh synthesis would have produced. Engine name *and* model: two
    engines can share a model name and produce different audio."""
    material = json.dumps(
        [engine, model, voice, round(float(speed), 3), lang, text, lead_silence_ms],
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AudioCache:
    """Disk cache with LRU-by-mtime eviction.

    State lives in the filesystem rather than in memory: the server restarts
    (compose `restart: always`) far more often than the cache turns over, and a
    warm cache across restarts is most of the value. mtime is the access time —
    `atime` is unreliable on `relatime` mounts, so a hit explicitly touches the
    file.
    """

    def __init__(self):
        self.enabled = config.CACHE_ENABLED
        self.dir = config.CACHE_DIR
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._evictions = 0
        if self.enabled:
            try:
                os.makedirs(self.dir, exist_ok=True)
            except OSError as e:
                log.warning("cannot create cache dir %s (%s); caching disabled", self.dir, e)
                self.enabled = False

    def _path(self, key):
        # One level of fan-out: a few thousand entries in one directory is fine
        # on ext4 but miserable to inspect by hand.
        return os.path.join(self.dir, key[:2], f"{key}.wav")

    def cacheable(self, text):
        return self.enabled and len(text) <= config.CACHE_MAX_TEXT

    def get(self, key):
        """Return (wav_bytes, path) on a hit, (None, None) on a miss."""
        if not self.enabled:
            return None, None
        path = self._path(key)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            with self._lock:
                self._misses += 1
            return None, None
        if not data:
            # A zero-length file means a previous write was interrupted; treat
            # it as a miss and let the next put overwrite it.
            with self._lock:
                self._misses += 1
            return None, None
        try:
            os.utime(path, None)
        except OSError:
            pass
        with self._lock:
            self._hits += 1
        return data, path

    def put(self, key, wav_bytes):
        """Store audio, returning its path (or None if not stored). Writes go to
        a temp name and are renamed, so a crash mid-write can never leave a
        truncated WAV that would later play as a burst of noise."""
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(wav_bytes)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("cache write failed for %s: %s", key[:12], e)
            return None
        with self._lock:
            self._writes += 1
        return path

    def path_if_present(self, key):
        if not self.enabled:
            return None
        path = self._path(key)
        return path if os.path.exists(path) else None

    def _entries(self):
        entries = []
        for root, _dirs, files in os.walk(self.dir):
            for name in files:
                if not name.endswith(".wav"):
                    continue
                full = os.path.join(root, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append((full, st.st_size, st.st_mtime))
        return entries

    def prune(self):
        """Enforce the age and size limits. Called on a timer, not on every put:
        walking the tree is cheap but not free, and being briefly over the limit
        costs nothing."""
        if not self.enabled:
            return 0
        entries = self._entries()
        removed = 0
        now = time.time()

        if config.CACHE_MAX_AGE_DAYS > 0:
            cutoff = now - config.CACHE_MAX_AGE_DAYS * 86400
            fresh = []
            for full, size, mtime in entries:
                if mtime < cutoff:
                    try:
                        os.unlink(full)
                        removed += 1
                    except OSError:
                        fresh.append((full, size, mtime))
                else:
                    fresh.append((full, size, mtime))
            entries = fresh

        if config.CACHE_MAX_MB > 0:
            limit = config.CACHE_MAX_MB * 1024 * 1024
            total = sum(size for _f, size, _m in entries)
            if total > limit:
                # Oldest access first — the LRU part.
                for full, size, _mtime in sorted(entries, key=lambda e: e[2]):
                    if total <= limit:
                        break
                    try:
                        os.unlink(full)
                        total -= size
                        removed += 1
                    except OSError:
                        pass

        if removed:
            with self._lock:
                self._evictions += removed
            log.info("cache pruned %d entries", removed)
        return removed

    def clear(self):
        count = 0
        for full, _size, _mtime in self._entries():
            try:
                os.unlink(full)
                count += 1
            except OSError:
                pass
        return count

    def stats(self):
        entries = self._entries() if self.enabled else []
        with self._lock:
            hits, misses = self._hits, self._misses
            writes, evictions = self._writes, self._evictions
        lookups = hits + misses
        return {
            "enabled": self.enabled,
            "entries": len(entries),
            "bytes": sum(size for _f, size, _m in entries),
            "max_mb": config.CACHE_MAX_MB,
            "hits": hits,
            "misses": misses,
            "writes": writes,
            "evictions": evictions,
            "hit_rate": round(hits / lookups, 3) if lookups else None,
        }


class VoiceListCache:
    """Voice lists change only when an engine is redeployed, but the dashboard
    asks on every load. A short TTL keeps it responsive without hammering the
    engines, and a stale list is served if the engine has since gone down —
    better a slightly old list than an empty picker."""

    def __init__(self, ttl):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._entries = {}

    def get(self, engine, fetch):
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(engine)
            if cached and now - cached[1] < self.ttl:
                return cached[0], None
        voices, error = fetch(engine)
        if error:
            with self._lock:
                cached = self._entries.get(engine)
            if cached:
                return cached[0], None
            return [], error
        with self._lock:
            self._entries[engine] = (voices, now)
        return voices, None

    def invalidate(self):
        with self._lock:
            self._entries.clear()


audio_cache = AudioCache()
voice_cache = VoiceListCache(config.VOICE_LIST_TTL)
