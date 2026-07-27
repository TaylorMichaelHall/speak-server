"""TTS backends, normalized to one call and one output format.

Every engine here speaks the OpenAI `/v1/audio/speech` dialect, so an engine is
little more than a base URL, the model name it insists on, the voice it uses
when the request doesn't name one, and the response format it will actually
produce. Voice names are engine-specific, which is why the default voice is
per-engine rather than global.

Two things layer on top of the plain call:

* **health** — consecutive failures put an engine in a cooldown so automatic
  selection (`random`, `fastest`) stops picking a backend that is down. An
  engine named explicitly by the caller is always tried anyway; they asked for
  it and deserve its real error rather than a silent substitution.
* **latency** — a rolling window of recent synthesis times per engine, which is
  what `ENGINE=fastest` selects on. Measured, not configured: which engine is
  quicker depends on the host and the load, not on which one looks faster.

A 4xx from an engine is deliberately *not* health: see `synthesize`.
"""

import io
import json
import logging
import statistics
import threading
import time
import urllib.error
import urllib.request
import wave
from collections import deque

import config

log = logging.getLogger("engines")

# Selection keywords a caller may pass as `engine` instead of a real name.
# Kept out of ENGINES so "is this a configured backend" stays a dict lookup.
PSEUDO_ENGINES = ("random", "fastest")


def _build_engines():
    engines = {
        "kokoro": {
            "url": config.KOKORO_URL,
            "model": "kokoro",
            "voice": config.DEFAULT_VOICE,
            "format": "wav",
            "voices_path": "/v1/audio/voices",
        },
        "supertonic": {
            "url": config.SUPERTONIC_URL,
            "model": "supertonic-3",
            "voice": config.SUPERTONIC_VOICE,
            "format": "wav",
            "voices_path": "/v1/styles",
        },
    }
    return engines


ENGINES = _build_engines()


# --------------------------------------------------------------------------
# health and latency
# --------------------------------------------------------------------------


class EngineStats:
    """Per-engine health and latency. One lock for all of them: updates are a
    few microseconds and happen once per synthesis, so contention is nil and a
    single lock is easier to reason about than one per engine."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {
            name: {
                "latencies": deque(maxlen=config.LATENCY_WINDOW),
                "failures": 0,
                "cooldown_until": 0.0,
                "last_error": None,
                "last_ok": None,
                "total_ok": 0,
                "total_failed": 0,
            }
            for name in ENGINES
        }

    def record_success(self, name, seconds):
        with self._lock:
            st = self._state[name]
            st["latencies"].append(seconds)
            st["failures"] = 0
            st["cooldown_until"] = 0.0
            st["last_error"] = None
            st["last_ok"] = time.time()
            st["total_ok"] += 1

    def record_failure(self, name, error):
        with self._lock:
            st = self._state[name]
            st["failures"] += 1
            st["last_error"] = error
            st["total_failed"] += 1
            if st["failures"] >= config.ENGINE_FAILURE_THRESHOLD:
                st["cooldown_until"] = time.time() + config.ENGINE_COOLDOWN

    def is_cooling_down(self, name):
        with self._lock:
            return self._state[name]["cooldown_until"] > time.time()

    def latency(self, name):
        """Median of the recent window, or None with nothing measured yet.
        Median rather than mean because one 30-second cold start shouldn't
        condemn an engine that is otherwise the fastest."""
        with self._lock:
            samples = list(self._state[name]["latencies"])
        return statistics.median(samples) if samples else None

    def snapshot(self):
        now = time.time()
        out = {}
        with self._lock:
            for name, st in self._state.items():
                samples = list(st["latencies"])
                out[name] = {
                    "latency_ms": round(statistics.median(samples) * 1000) if samples else None,
                    "samples": len(samples),
                    "failures": st["failures"],
                    "cooling_down": st["cooldown_until"] > now,
                    "cooldown_remaining": max(0, round(st["cooldown_until"] - now)),
                    "last_error": st["last_error"],
                    "last_ok": st["last_ok"],
                    "total_ok": st["total_ok"],
                    "total_failed": st["total_failed"],
                }
        return out


stats = EngineStats()


# --------------------------------------------------------------------------
# format normalization
# --------------------------------------------------------------------------


def wav_duration_ms(wav_bytes):
    """Playback length, used to bound waits and to show clip length in history.
    None if the bytes aren't parseable as WAV."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as src:
            rate = src.getframerate()
            frames = len(src.readframes(-1)) // (src.getsampwidth() * src.getnchannels())
        return round(frames * 1000 / rate) if rate else None
    except (wave.Error, EOFError, ValueError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------
# synthesis
# --------------------------------------------------------------------------


def synthesize(engine, text, voice=None, speed=1.0, lang=None):
    """Ask one engine for audio, normalized to WAV.

    Returns (wav_bytes, None, seconds) on success and (None, error, seconds) on
    failure — no exceptions escape, because a caller in the player thread has
    nowhere useful to put them and a failed engine is an ordinary outcome here.
    """
    cfg = ENGINES[engine]
    effective_voice = cfg["voice"] if voice is None else voice
    payload = {
        "model": cfg["model"],
        "input": text,
        "voice": effective_voice,
        "response_format": cfg["format"],
        "speed": speed,
    }
    # Supertonic extension ('ko', 'ja', ..., default auto-fallback 'na'); only
    # sent when given, so kokoro never sees an unknown field.
    if lang is not None and engine == "supertonic":
        payload["lang"] = lang

    headers = {"Content-Type": "application/json"}

    req = urllib.request.Request(
        f"{cfg['url']}/v1/audio/speech",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    started = time.monotonic()

    def fail(message, healthy=False):
        """`healthy` marks a refusal that says nothing about the engine's health.

        An engine that answers 4xx is demonstrably up — it read the request and
        rejected it. Counting that as a failure lets a caller's mistake bench a
        working backend: asking for `random` with a kokoro voice makes supertonic
        return "unknown voice", and three of those trip the cooldown. The engine
        is then skipped by automatic selection, its latency ranking rots, and the
        dashboard reports a healthy engine as failing.
        """
        elapsed = time.monotonic() - started
        if healthy:
            log.warning("%s refused the request in %.2fs: %s", engine, elapsed, message)
        else:
            stats.record_failure(engine, message)
            log.warning("%s failed in %.2fs: %s", engine, elapsed, message)
        return None, message, elapsed

    try:
        with urllib.request.urlopen(req, timeout=config.SYNTH_TIMEOUT) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        return fail(f"{engine} returned HTTP {e.code}: {detail}",
                    healthy=400 <= e.code < 500)
    except (urllib.error.URLError, OSError) as e:
        return fail(f"{engine} unreachable at {cfg['url']}: {e}")

    if not audio:
        return fail(f"{engine} returned empty audio")

    elapsed = time.monotonic() - started
    stats.record_success(engine, elapsed)
    log.debug("%s synthesized %d bytes in %.2fs", engine, len(audio), elapsed)
    return audio, None, elapsed


def resolve_candidates(engine):
    """Turn the requested engine into the ordered list to try.

    Returns (candidates, error). The ordering encodes the fallback policy:

    * `random` — engines shuffled, so a knocked-out engine costs variety
      rather than speech.
    * `fastest` — engines by measured latency, unmeasured ones first so
      every engine gets sampled before the ranking settles.
    * a named engine — just that one. No peer fallback: substituting another
      engine's voice would misreport what happened.

    Engines in failure cooldown are pushed to the back of an automatic
    selection instead of removed, so a total outage still produces a real error
    from a real attempt rather than "no engines available".
    """
    import random as _random  # local: only automatic selection needs it

    if engine in ENGINES:
        return [engine], None

    names = list(ENGINES)
    if not names:
        return [], "no engines are configured"

    if engine == "random":
        candidates = _random.sample(names, len(names))
    elif engine == "fastest":
        candidates = sorted(
            names,
            key=lambda n: (stats.latency(n) is not None, stats.latency(n) or 0.0),
        )
    else:
        options = ", ".join(list(ENGINES) + list(PSEUDO_ENGINES))
        return [], f"unknown engine {engine!r}; one of: {options}"

    candidates.sort(key=stats.is_cooling_down)
    return candidates, None


# --------------------------------------------------------------------------
# voice listing (for the dashboard and /api/voices)
# --------------------------------------------------------------------------


def _extract_voices(payload):
    """Engines disagree on the shape: kokoro answers {"voices": [...]},
    supertonic's /v1/styles answers a list or {"styles": [...]}. Pull names out
    of whichever arrived rather than special-casing per engine, so a new
    OpenAI-dialect engine works without code."""
    if isinstance(payload, dict):
        for key in ("voices", "styles", "data"):
            if key in payload:
                payload = payload[key]
                break
        else:
            payload = list(payload)
    if not isinstance(payload, list):
        return []
    out = []
    for item in payload:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for key in ("name", "id", "voice", "style"):
                if isinstance(item.get(key), str):
                    out.append(item[key])
                    break
    return out


def fetch_voices(engine):
    """Ask an engine what voices it has. Returns (voices, error)."""
    cfg = ENGINES[engine]
    path = cfg.get("voices_path")
    if not path:
        # An engine that can't enumerate its voices still has the one this
        # server is configured to use, which beats reporting none at all.
        return [cfg["voice"]], None
    req = urllib.request.Request(f"{cfg['url']}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _extract_voices(json.loads(resp.read().decode())), None
    except (urllib.error.URLError, OSError, ValueError) as e:
        return [], f"{engine}: {e}"
