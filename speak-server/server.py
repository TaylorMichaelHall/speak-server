"""Speak server: POST text -> synthesize via a TTS engine -> play on host speakers.

Runs in a container with the host's Pulse socket mounted. Playback is
serialized (single-threaded HTTPServer) so overlapping requests queue
instead of talking over each other. Errors map to non-2xx so remote
callers never believe they spoke when nothing played.
"""

import io
import json
import os
import subprocess
import urllib.error
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

KOKORO_URL = os.environ.get("KOKORO_URL", "http://kokoro:8880")
SUPERTONIC_URL = os.environ.get("SUPERTONIC_URL", "http://supertonic:7788")
DEFAULT_ENGINE = os.environ.get("ENGINE", "kokoro")
DEFAULT_VOICE = os.environ.get("VOICE", "af_heart")
SUPERTONIC_VOICE = os.environ.get("SUPERTONIC_VOICE", "M1")
PORT = int(os.environ.get("PORT", "8899"))

# Both engines speak the OpenAI /v1/audio/speech dialect, so an engine is just
# a base URL, the model name it insists on, and the voice used when the
# request doesn't name one (voice names are engine-specific, so a global
# default would be wrong for one of them).
ENGINES = {
    "kokoro": {"url": KOKORO_URL, "model": "kokoro", "voice": DEFAULT_VOICE},
    "supertonic": {"url": SUPERTONIC_URL, "model": "supertonic-3", "voice": SUPERTONIC_VOICE},
}
# The audio sink suspends when idle; opening a stream spends the first few
# hundred ms resuming, which clips the start of speech. Prepend silence so the
# resume ramp eats that instead of the first syllable. Silent, so it's never
# heard and never missed. Set to 0 to disable.
LEAD_SILENCE_MS = int(os.environ.get("LEAD_SILENCE_MS", "500"))


def prepend_silence(wav_bytes, ms):
    """Return a WAV with `ms` of leading silence. Falls back to the original
    bytes if anything about the WAV can't be parsed — padding is a nicety, so
    a parse failure must never stop playback."""
    if ms <= 0:
        return wav_bytes
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as src:
            nchannels = src.getnchannels()
            sampwidth = src.getsampwidth()
            framerate = src.getframerate()
            # kokoro returns a streaming WAV whose header carries a placeholder
            # frame count, so read to EOF rather than trusting getnframes().
            frames = src.readframes(-1)
        pad_frames = int(framerate * ms / 1000)
        silence = b"\x00" * (pad_frames * sampwidth * nchannels)
        out = io.BytesIO()
        with wave.open(out, "wb") as dst:
            # Set format explicitly (not setparams) so the writer sizes the
            # header from the bytes actually written, not the placeholder count.
            dst.setnchannels(nchannels)
            dst.setsampwidth(sampwidth)
            dst.setframerate(framerate)
            dst.writeframes(silence + frames)
        return out.getvalue()
    except (wave.Error, EOFError, ValueError):
        return wav_bytes


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, message):
        body = (message.rstrip("\n") + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, "ok")
        else:
            self._reply(404, "not found")

    def do_POST(self):
        if self.path.split("?")[0] != "/speak":
            self._reply(404, "not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")

        # Body is plain text, or JSON
        # {"text": ..., "engine": ..., "voice": ..., "speed": ..., "lang": ...}
        text, engine, voice, speed, lang = raw, DEFAULT_ENGINE, None, 1.0, None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "text" in parsed:
                text = str(parsed["text"])
                engine = str(parsed.get("engine", DEFAULT_ENGINE))
                if "voice" in parsed:
                    voice = str(parsed["voice"])
                speed = float(parsed.get("speed", 1.0))
                if "lang" in parsed:
                    lang = str(parsed["lang"])
        except (ValueError, TypeError):
            pass

        if not text.strip():
            self._reply(400, "no text given")
            return

        if engine not in ENGINES:
            self._reply(400, f"unknown engine {engine!r}; one of: {', '.join(ENGINES)}")
            return
        cfg = ENGINES[engine]

        payload = {
            "model": cfg["model"],
            "input": text,
            "voice": voice if voice is not None else cfg["voice"],
            "response_format": "wav",
            "speed": speed,
        }
        # Supertonic extension ('ko', 'ja', ..., default auto-fallback 'na');
        # only sent when given so kokoro never sees an unknown field.
        if lang is not None:
            payload["lang"] = lang

        req = urllib.request.Request(
            f"{cfg['url']}/v1/audio/speech",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                audio = resp.read()
        except urllib.error.HTTPError as e:
            self._reply(502, f"{engine} returned HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
            return
        except (urllib.error.URLError, OSError) as e:
            self._reply(502, f"{engine} unreachable at {cfg['url']}: {e}")
            return

        if not audio:
            self._reply(502, f"{engine} returned empty audio")
            return

        audio = prepend_silence(audio, LEAD_SILENCE_MS)

        play = subprocess.run(
            ["paplay", "--client-name=speak-server", "/dev/stdin"],
            input=audio,
            capture_output=True,
            timeout=600,
        )
        if play.returncode != 0:
            self._reply(500, f"paplay failed ({play.returncode}): {play.stderr[:300].decode(errors='replace')}")
            return

        self._reply(200, "spoke")


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
