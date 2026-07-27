"""The HTTP surface.

`POST /speak` keeps its original contract exactly: a plain-text or JSON body, and
a response that arrives only once the audio has finished, where 2xx means it was
really spoken. Everything added here — priorities, async submission, the queue
controls, history, the dashboard — is reached through new fields or new paths, so
no existing client needs to change.

The server is threaded now. Playback is serialized by the player thread instead
of by the HTTP server, which means a health check no longer waits behind a
sentence someone is having read to them.
"""

import hmac
import json
import logging
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import audio
import auth
import cache
import config
import engines
import webhooks
from history import history
from queues import ValidationError, build_utterance, queue
from quiethours import quiet_hours

log = logging.getLogger("api")

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")

# Ceiling on how long a synchronous /speak may block. Sized from the stages it
# actually waits through, so raising SYNTH_TIMEOUT or PLAY_TIMEOUT doesn't
# silently start timing out at the HTTP layer.
def sync_wait_timeout():
    return config.SYNTH_TIMEOUT + config.PLAY_TIMEOUT + max(config.QUEUE_ITEM_TTL, 30) + 10


# Terminal status -> (HTTP code, whether it counts as spoken). The original codes
# are preserved: 502 when synthesis failed, 500 when playback did.
STATUS_CODES = {
    "spoke": 200,
    "failed": 502,
    "muted": 409,
    "interrupted": 409,
    "cancelled": 409,
    "dropped": 409,
    "expired": 409,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "speak-server"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ---- plumbing -----------------------------------------------------

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler writes to stderr unconditionally; route it
        # through logging so LOG_LEVEL applies and the format matches.
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body, content_type, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # A client that hung up mid-sentence is normal for a long
                # blocking /speak; nothing to report.
                pass

    def _text(self, code, message, extra_headers=None):
        body = (message.rstrip("\n") + "\n").encode("utf-8", errors="replace")
        self._send(code, body, "text/plain; charset=utf-8", extra_headers)

    def _json(self, code, payload, extra_headers=None):
        body = json.dumps(payload, default=str).encode()
        self._send(code, body, "application/json", extra_headers)

    def _reply(self, code, message, extra_headers=None):
        """Answer in whatever the caller speaks. curl and shell scripts want a
        line of text; the dashboard and other programs want JSON."""
        if self._wants_json():
            self._json(code, {"ok": 200 <= code < 300, "message": message}, extra_headers)
        else:
            self._text(code, message, extra_headers)

    def _wants_json(self):
        if self.path.startswith("/api/"):
            return True
        accept = self.headers.get("Accept", "")
        return "application/json" in accept and "text/plain" not in accept

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return ""
        # Cap the read: MAX_TEXT bounds useful bodies, and a webhook payload is
        # bigger than its text, so allow generous headroom but not unbounded.
        limit = max(config.MAX_TEXT * 8, 65536)
        if length > limit:
            raise ValidationError(f"body is {length} bytes; limit is {limit}", 413)
        return self.rfile.read(length).decode("utf-8", errors="replace")

    def _json_body(self):
        raw = self._body()
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ValidationError("body is not valid JSON") from None
        return parsed

    # ---- auth ---------------------------------------------------------

    def _authenticate(self, query):
        """Returns an Identity, or replies and returns None.

        `/health` never reaches here: a monitoring probe shouldn't need a
        credential, and it reveals nothing beyond "this process is alive".
        """
        try:
            identity, _source = auth.identify(
                self.headers, query, self.client_address[0]
            )
        except auth.AuthError as e:
            self._reply(401, str(e), {"WWW-Authenticate": 'Bearer realm="speak-server"'})
            return None
        return identity

    # ---- routing ------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/health":
                return self._health(query)

            identity = self._authenticate(query)
            if identity is None:
                return None

            handler = self._lookup(method, path)
            if handler is None:
                return self._reply(404, "not found")
            return handler(identity, path, query)
        except ValidationError as e:
            return self._reply(getattr(e, "status", 400), str(e))
        except auth.RateLimitError as e:
            return self._reply(429, str(e), {"Retry-After": str(e.retry_after)})
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception:
            log.exception("unhandled error handling %s %s", method, path)
            return self._reply(500, "internal error")

    def _lookup(self, method, path):
        exact = {
            ("POST", "/speak"): self._speak,
            ("POST", "/api/speak"): self._speak,
            ("GET", "/api/status"): self._status,
            ("GET", "/api/queue"): self._queue_get,
            ("POST", "/api/queue/clear"): self._queue_clear,
            ("POST", "/api/stop"): self._stop,
            ("POST", "/api/skip"): self._skip,
            ("POST", "/api/mute"): self._mute,
            ("GET", "/api/history"): self._history_list,
            ("GET", "/api/voices"): self._voices,
            ("GET", "/api/sinks"): self._sinks,
            ("GET", "/api/webhooks"): self._webhooks_list,
            ("POST", "/api/webhooks/reload"): self._webhooks_reload,
            ("POST", "/api/quiet/snooze"): self._quiet_snooze,
            ("POST", "/api/quiet/resume"): self._quiet_resume,
            ("POST", "/api/cache/clear"): self._cache_clear,
            ("GET", "/"): self._dashboard,
        }
        if (method, path) in exact:
            return exact[(method, path)]

        parts = [p for p in path.split("/") if p]
        if method == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "queue"]:
            return lambda i, p, q: self._queue_cancel(parts[2])
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "history"]:
            return lambda i, p, q: self._history_one(parts[2])
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "history"] \
                and parts[3] == "audio":
            return lambda i, p, q: self._history_audio(parts[2])
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "history"] \
                and parts[3] == "replay":
            return lambda i, p, q: self._history_replay(parts[2], i)
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "voices"]:
            return lambda i, p, q: self._voices(i, p, q, engine=parts[2])
        if method == "POST" and len(parts) == 2 and parts[0] == "webhook":
            return lambda i, p, q: self._webhook(parts[1], i, q)
        if method == "GET" and parts and parts[0] == "dashboard":
            return lambda i, p, q: self._static("/".join(parts[1:]))
        return None

    # ---- health & status ----------------------------------------------

    def _health(self, query):
        """Plain `ok` by default — that is the documented contract and what
        compose healthchecks grep for. `?verbose=1` adds detail for humans."""
        if not query.get("verbose"):
            return self._text(200, "ok")
        snapshot = queue.snapshot()
        return self._json(200, {
            "ok": True,
            "engines": engines.stats.snapshot(),
            "queue_depth": snapshot["depth"],
            "playing": bool(snapshot["playing"]),
            "muted": snapshot["muted"],
            "quiet_hours_active": quiet_hours.status()["active"],
        })

    def _status(self, identity, path, query):
        import mqtt  # deferred: importing paho at module load is wasted when unused

        snapshot = queue.snapshot()
        return self._json(200, {
            "queue": snapshot,
            "engines": engines.stats.snapshot(),
            "quiet_hours": quiet_hours.status(),
            "cache": cache.audio_cache.stats(),
            "history": history.stats(),
            "auth": auth.status(),
            "mqtt": mqtt.bridge.status(),
            "webhooks": webhooks.registry.public(),
            "audio": {
                "default_sink": audio.default_sink(),
                "configured_sink": config.AUDIO_SINK or None,
                "routes": config.AUDIO_ROUTES,
                "volume": config.VOLUME,
                "muted": snapshot["muted"],
                "lead_silence_ms": config.LEAD_SILENCE_MS,
            },
            "config": {
                "default_engine": config.DEFAULT_ENGINE,
                "default_priority": config.DEFAULT_PRIORITY,
                "priorities": config.PRIORITIES,
                "engines": {
                    name: {"model": cfg["model"], "voice": cfg["voice"],
                           "url": cfg["url"]}
                    for name, cfg in engines.ENGINES.items()
                },
                "interrupt": config.INTERRUPT,
                "interrupt_min_gap": config.INTERRUPT_MIN_GAP,
                "interrupt_requeue": config.INTERRUPT_REQUEUE,
                "queue_max": config.QUEUE_MAX,
                "queue_item_ttl": config.QUEUE_ITEM_TTL,
                "max_text": config.MAX_TEXT,
            },
            "identity": {"name": identity.name, "authenticated": identity.authenticated},
        })

    # ---- speaking -----------------------------------------------------

    def _speak(self, identity, path, query):
        raw = self._body()
        # Plain text or JSON, as before. A body that isn't JSON is text — that is
        # how `curl --data "hello"` has always worked here.
        payload = None
        stripped = raw.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    payload = parsed
            except ValueError:
                payload = None
        if payload is None:
            payload = {"text": raw}
        # Query-string overrides make one-liners possible without jq:
        #   curl -X POST --data 'text' 'http://host:8899/speak?priority=high'
        for key in ("engine", "voice", "speed", "lang", "priority",
                    "sink", "volume", "wait"):
            if key in query and key not in payload:
                payload[key] = query[key][0]

        item = build_utterance(payload, source="http", client=identity.name)
        auth.check_rate_limit(identity, item.priority)
        return self._submit_and_reply(item)

    def _submit_and_reply(self, item):
        submission = queue.submit(item)

        if submission.status == "dropped":
            code = 503 if "full" in (submission.reason or "") else 409
            return self._reply(code, f"not spoken: {submission.reason}")
        if submission.status == "deferred":
            return self._reply(202, f"deferred until quiet hours end ({item.id})")
        if not item.wait:
            if self._wants_json():
                return self._json(202, {"ok": True, "status": "queued", **item.public()})
            return self._text(202, f"queued {item.id}")

        if not item.done.wait(sync_wait_timeout()):
            return self._reply(504, f"timed out waiting for {item.id} to be spoken")

        # The item may have been deferred *after* being queued (quiet hours
        # started while it waited), in which case it is still going to be spoken
        # later and 202 is the honest answer.
        if item.status == "deferred":
            return self._reply(202, f"deferred until quiet hours end ({item.id})")
        code = STATUS_CODES.get(item.status, 500)
        if item.status == "failed" and item.fail_stage == "playback":
            code = 500
        if item.status == "spoke":
            if self._wants_json():
                return self._json(200, {"ok": True, **item.public()})
            return self._text(200, "spoke")
        return self._reply(code, f"{item.status}: {item.error or 'no detail'}")

    # ---- queue control ------------------------------------------------

    def _queue_get(self, identity, path, query):
        return self._json(200, queue.snapshot())

    def _queue_cancel(self, item_id):
        result = queue.cancel(item_id)
        if result == "not found":
            return self._reply(404, f"no queued utterance {item_id}")
        return self._reply(200, result)

    def _queue_clear(self, identity, path, query):
        return self._reply(200, f"cleared {queue.clear()} queued utterances")

    def _stop(self, identity, path, query):
        """Stop what's playing *and* drop the queue — the panic button. `skip` is
        the one that only ends the current utterance."""
        cleared = queue.clear()
        stopped = queue.skip()
        return self._reply(200, f"{'stopped playback, ' if stopped else ''}"
                                f"cleared {cleared} queued")

    def _skip(self, identity, path, query):
        return self._reply(200, "skipped" if queue.skip() else "nothing playing")

    def _mute(self, identity, path, query):
        payload = self._json_body() if self.headers.get("Content-Length") else {}
        if "muted" in payload:
            muted = bool(payload["muted"])
        elif "muted" in query:
            muted = query["muted"][0].lower() not in ("0", "false", "no", "off")
        else:
            muted = not audio.player.muted
        audio.player.set_muted(muted)
        return self._reply(200, "muted" if muted else "unmuted")

    def _quiet_snooze(self, identity, path, query):
        payload = self._json_body() if self.headers.get("Content-Length") else {}
        seconds = payload.get("seconds") or (query.get("seconds") or [None])[0]
        try:
            seconds = float(seconds) if seconds is not None else None
        except (TypeError, ValueError):
            raise ValidationError("seconds must be a number") from None
        quiet_hours.snooze(seconds)
        return self._reply(200, f"quiet hours snoozed for {seconds or 'ever'}")

    def _quiet_resume(self, identity, path, query):
        quiet_hours.unsnooze()
        return self._reply(200, "quiet hours re-enabled")

    def _cache_clear(self, identity, path, query):
        removed = cache.audio_cache.clear()
        cache.voice_cache.invalidate()
        return self._reply(200, f"cleared {removed} cached clips")

    # ---- history ------------------------------------------------------

    def _history_list(self, identity, path, query):
        def first(name, default=None):
            return (query.get(name) or [default])[0]

        def number(name, default):
            try:
                return int(first(name) or default)
            except (TypeError, ValueError):
                raise ValidationError(f"{name} must be a whole number") from None

        rows = history.query(
            limit=number("limit", 50),
            offset=number("offset", 0),
            status=first("status"),
            search=first("q"),
            source=first("source"),
        )
        return self._json(200, {"rows": rows, "stats": history.stats()})

    def _history_one(self, row_id):
        row = history.get(row_id)
        if row is None:
            return self._reply(404, "no such history entry")
        return self._json(200, row)

    def _history_audio(self, row_id):
        data = history.clip_bytes(row_id)
        if data is None:
            return self._reply(404, "no audio kept for that entry")
        # Cacheable: a history row's audio never changes, and the dashboard's
        # <audio> element will re-request it on every seek otherwise.
        return self._send(200, data, "audio/wav",
                          {"Cache-Control": "private, max-age=3600"})

    def _history_replay(self, row_id, identity):
        """Say a past utterance again. Deliberately re-runs the whole pipeline
        rather than pushing the stored clip at the speakers: the cache will make
        it instant anyway, and this way a replay honours the current quiet hours,
        mute state and routing instead of bypassing them."""
        row = history.get(row_id)
        if row is None:
            return self._reply(404, "no such history entry")
        payload = {
            "text": row["text"],
            "engine": row.get("engine_used") or row.get("engine"),
            "voice": row.get("voice"),
            "speed": row.get("speed") or 1.0,
            "lang": row.get("lang"),
            "sink": row.get("sink"),
            "priority": config.priority_name(row.get("priority") or 2),
            "wait": False,
        }
        item = build_utterance({k: v for k, v in payload.items() if v is not None},
                               source="dashboard", client=identity.name)
        auth.check_rate_limit(identity, item.priority)
        return self._submit_and_reply(item)

    # ---- engines & audio devices ---------------------------------------

    def _voices(self, identity, path, query, engine=None):
        names = [engine] if engine else list(engines.ENGINES)
        out, errors = {}, {}
        for name in names:
            if name not in engines.ENGINES:
                return self._reply(404, f"unknown engine {name!r}")
            voices, error = cache.voice_cache.get(name, engines.fetch_voices)
            out[name] = voices
            if error:
                errors[name] = error
        return self._json(200, {"voices": out, "errors": errors,
                                "defaults": {n: engines.ENGINES[n]["voice"] for n in names}})

    def _sinks(self, identity, path, query):
        return self._json(200, {
            "sinks": audio.list_sinks(),
            "default": audio.default_sink(),
            "routes": config.AUDIO_ROUTES,
        })

    # ---- webhooks -----------------------------------------------------

    def _webhooks_list(self, identity, path, query):
        return self._json(200, webhooks.registry.public())

    def _webhooks_reload(self, identity, path, query):
        webhooks.registry.load()
        return self._json(200, webhooks.registry.public())

    def _webhook(self, name, identity, query):
        receiver = webhooks.registry.get(name)
        if receiver is None:
            return self._reply(404, f"no webhook receiver {name!r}")
        if receiver.secret:
            # A per-receiver secret is the only credential most senders can
            # carry, so it stands in for the normal token here.
            supplied = (self.headers.get("X-Webhook-Secret")
                        or (query.get("secret") or [""])[0])
            if not hmac.compare_digest(supplied, receiver.secret):
                log.warning("webhook %s: bad secret from %s", name, identity.address)
                return self._reply(401, "invalid webhook secret")

        raw = self._body()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            # Not everything sends JSON; treat the body as the sentence.
            payload = raw

        if isinstance(payload, dict) and not receiver.matches(payload):
            return self._reply(200, "ignored: payload did not match this receiver's filter")

        item = receiver.build(payload, client=f"webhook:{name}")
        auth.check_rate_limit(identity, item.priority)
        return self._submit_and_reply(item)

    # ---- dashboard ----------------------------------------------------

    def _dashboard(self, identity, path, query):
        if not config.DASHBOARD_ENABLED:
            return self._reply(404, "dashboard is disabled (DASHBOARD=0)")
        return self._static("index.html", token=(query.get("token") or [None])[0])

    def _static(self, relative, token=None):
        if not config.DASHBOARD_ENABLED:
            return self._reply(404, "dashboard is disabled (DASHBOARD=0)")
        relative = relative or "index.html"
        # Resolve and confirm the result is still inside the dashboard directory:
        # the only defence against `..` that actually holds on every platform.
        full = os.path.realpath(os.path.join(DASHBOARD_DIR, relative))
        if not full.startswith(os.path.realpath(DASHBOARD_DIR) + os.sep):
            return self._reply(403, "forbidden")
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._reply(404, "not found")
        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        headers = {}
        if token:
            # The dashboard was opened with ?token=…; hand it to the browser as a
            # cookie so its API calls authenticate and the URL can be cleaned up.
            headers["Set-Cookie"] = (
                f"speak_token={urllib.parse.quote(token)}; Path=/; "
                "HttpOnly; SameSite=Strict; Max-Age=2592000"
            )
        return self._send(200, body, content_type, headers)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Without this, a restart within TIME_WAIT fails to bind — and this server
    # restarts on every config change.
    allow_reuse_address = True


def serve():
    server = Server((config.BIND, config.PORT), Handler)
    log.info("listening on %s:%d", config.BIND, config.PORT)
    return server
