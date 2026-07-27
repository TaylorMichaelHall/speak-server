"""The speech queue: one player thread, priority ordering, interruption, and the
synthesis pipeline.

Playback used to be serialized by running a single-threaded HTTP server, which
worked but coupled two unrelated things: it also meant one slow client could
block a health check. Serialization now lives here, in a single player thread
consuming a priority queue, and the HTTP server is free to be threaded.

Ordering is (priority, arrival). Nothing ages up: a `low` item never overtakes a
`normal` one, because these are announcements and an old one is usually less
interesting than a new one, not more. What keeps `low` from starving is the TTL —
it expires rather than waiting forever behind more important speech.

The queue is deliberately small (`QUEUE_MAX`, default 100) and scanned linearly.
A heap would be asymptotically better and practically worse: items become
runnable at different times (quiet-hours deferral) and operators cancel by id,
both of which a flat list does in a few lines and correctly.
"""

import logging
import secrets
import threading
import time

import audio
import cache
import config
import engines
from history import history
from quiethours import quiet_hours

log = logging.getLogger("queue")

# How long the player thread sleeps when the queue holds nothing runnable. Short
# enough that a deferred item starts within a second of becoming due, long
# enough to be invisible on a CPU graph.
TICK_SECONDS = 0.5

# An interrupted-and-requeued utterance gets a bounded number of replays. Without
# a cap, two clients trading high-priority alerts could keep restarting the same
# clip forever and it would never actually be heard.
MAX_REPLAYS = 3


class Utterance:
    __slots__ = (
        "id", "text", "engine", "voice", "speed", "lang", "priority",
        "sink", "volume", "source", "client", "created_at", "seq", "not_before",
        "expires_at", "wait", "done", "status", "error", "engine_used",
        "synth_ms", "play_ms", "audio_ms", "queue_ms", "cache_hit", "replays",
        "clip_path", "history_id", "fail_stage", "quiet_deferred",
    )

    def __init__(self, text, **kw):
        self.id = secrets.token_hex(6)
        self.text = text
        self.engine = kw.get("engine") or config.DEFAULT_ENGINE
        self.voice = kw.get("voice")
        self.speed = kw.get("speed", 1.0)
        self.lang = kw.get("lang")
        self.priority = kw.get("priority", config.priority_value(config.DEFAULT_PRIORITY, 2))
        self.sink = kw.get("sink")
        self.volume = kw.get("volume")
        self.source = kw.get("source", "http")
        self.client = kw.get("client")
        self.wait = kw.get("wait", True)
        self.created_at = time.time()
        self.seq = 0
        self.not_before = None
        # Whether `not_before` was set by quiet hours, as opposed to any other
        # reason to hold something back. Only a quiet-hours deferral should be
        # undone by snoozing quiet hours.
        self.quiet_deferred = False
        self.expires_at = (
            self.created_at + config.QUEUE_ITEM_TTL if config.QUEUE_ITEM_TTL > 0 else None
        )
        self.done = threading.Event()
        self.status = "queued"
        self.error = None
        self.engine_used = None
        self.synth_ms = None
        self.play_ms = None
        self.audio_ms = None
        self.queue_ms = None
        self.cache_hit = False
        self.replays = 0
        self.clip_path = None
        self.history_id = None
        # "synthesis" or "playback" — the original API distinguished these with
        # 502 vs 500, and that distinction is genuinely useful ("the engine is
        # down" vs "there is no desktop audio session"), so it survives.
        self.fail_stage = None

    def public(self):
        """The shape the API and MQTT publish. Text is included in full: the
        dashboard shows it, and truncating here would mean the queue view and
        the history view disagree about what was said."""
        return {
            "id": self.id,
            "text": self.text,
            "engine": self.engine,
            "engine_used": self.engine_used,
            "voice": self.voice,
            "speed": self.speed,
            "lang": self.lang,
            "priority": config.priority_name(self.priority),
            "priority_value": self.priority,
            "sink": self.sink,
            "volume": self.volume,
            "source": self.source,
            "client": self.client,
            "status": self.status,
            "error": self.error,
            "fail_stage": self.fail_stage,
            "created_at": self.created_at,
            "not_before": self.not_before,
            "expires_at": self.expires_at,
            "queue_ms": self.queue_ms,
            "synth_ms": self.synth_ms,
            "play_ms": self.play_ms,
            "audio_ms": self.audio_ms,
            "cache_hit": self.cache_hit,
            "replays": self.replays,
            "history_id": self.history_id,
        }


class ValidationError(ValueError):
    """A payload the caller has to fix. Carries an HTTP status so the three
    front ends (HTTP, MQTT, webhooks) don't each invent their own mapping."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def build_utterance(payload, source="http", client=None):
    """Turn a request body into a validated Utterance.

    Every front end goes through here, so `speak/say` over MQTT and a webhook
    POST accept exactly the same fields as `POST /speak` and reject the same
    things. Raises ValidationError on bad input.
    """
    if isinstance(payload, str):
        payload = {"text": payload}
    if not isinstance(payload, dict):
        raise ValidationError("body must be text or a JSON object")

    text = payload.get("text")
    if text is None:
        raise ValidationError("no text given")
    text = str(text)
    if not text.strip():
        raise ValidationError("no text given")
    if len(text) > config.MAX_TEXT:
        raise ValidationError(
            f"text is {len(text)} characters; MAX_TEXT is {config.MAX_TEXT}", 413
        )

    engine = str(payload.get("engine") or config.DEFAULT_ENGINE)
    if engine not in engines.ENGINES and engine not in engines.PSEUDO_ENGINES:
        options = ", ".join(list(engines.ENGINES) + list(engines.PSEUDO_ENGINES))
        raise ValidationError(f"unknown engine {engine!r}; one of: {options}")

    try:
        speed = float(payload.get("speed", 1.0))
    except (TypeError, ValueError):
        raise ValidationError(f"speed {payload.get('speed')!r} is not a number") from None
    if not config.SPEED_MIN <= speed <= config.SPEED_MAX:
        raise ValidationError(
            f"speed {speed} is outside {config.SPEED_MIN}-{config.SPEED_MAX}"
        )

    priority = config.priority_value(payload.get("priority", config.DEFAULT_PRIORITY))
    if priority is None:
        raise ValidationError(
            f"unknown priority {payload.get('priority')!r}; "
            f"one of: {', '.join(config.PRIORITIES)}"
        )

    volume = payload.get("volume")
    if volume is not None:
        try:
            volume = int(volume)
        except (TypeError, ValueError):
            raise ValidationError(f"volume {volume!r} is not a number") from None
        if not 0 <= volume <= 100:
            raise ValidationError(f"volume {volume} is outside 0-100")

    # `wait` defaults to true so the original blocking contract is unchanged:
    # existing clients POST and get 200 once the audio has finished.
    wait = payload.get("wait", True)
    if isinstance(wait, str):
        wait = wait.strip().lower() not in ("0", "false", "no", "off")

    return Utterance(
        text,
        engine=engine,
        voice=str(payload["voice"]) if payload.get("voice") else None,
        speed=speed,
        lang=str(payload["lang"]) if payload.get("lang") else None,
        priority=priority,
        sink=str(payload["sink"]) if payload.get("sink") else None,
        volume=volume,
        source=source,
        client=client,
        wait=bool(wait),
    )


class Submission:
    """Outcome of handing an utterance to the queue, before anything is spoken."""

    __slots__ = ("accepted", "status", "reason", "item")

    def __init__(self, accepted, status, reason=None, item=None):
        self.accepted = accepted
        self.status = status
        self.reason = reason
        self.item = item


class SpeechQueue:
    def __init__(self, player=None, quiet=None):
        self.player = player or audio.player
        self.quiet = quiet or quiet_hours
        self._lock = threading.Condition()
        self._items = []
        self._current = None
        self._seq = 0
        self._shutdown = threading.Event()
        self._thread = None
        self._listeners = []
        self._counts = {}
        # Separate from the queue condition: counters are bumped from
        # _finalize(), which runs on HTTP threads too (a dropped submission
        # finalizes inline) and must never take the queue lock.
        self._counts_lock = threading.Lock()

    # ---- events -------------------------------------------------------

    def on_event(self, callback):
        """Register a `callback(event_name, payload_dict)`. Used by MQTT to
        publish and by nothing else; kept generic so a second consumer doesn't
        require touching the player loop."""
        self._listeners.append(callback)

    def _emit(self, event, payload):
        for callback in list(self._listeners):
            try:
                callback(event, payload)
            except Exception:
                # A broken listener must never take down the player thread —
                # speech is the point, telemetry is not.
                log.exception("event listener failed for %s", event)

    def _count(self, status):
        with self._counts_lock:
            self._counts[status] = self._counts.get(status, 0) + 1

    # ---- submission ---------------------------------------------------

    def submit(self, item):
        """Accept (or refuse) an utterance. Never blocks on playback."""
        action, detail = self.quiet.decide(item.priority)
        if action == "drop":
            item.status = "dropped"
            item.error = detail
            self._finalize(item, play_skipped=True)
            item.done.set()
            return Submission(False, "dropped", detail, item)
        if action == "attenuate":
            # Take the quieter of the two: a client asking for volume 100 during
            # quiet hours is exactly the case the policy exists to prevent.
            requested = config.VOLUME if item.volume is None else item.volume
            item.volume = min(requested, detail)
        elif action == "defer":
            item.not_before = detail
            item.quiet_deferred = True
            # The TTL measures time spent *waiting to be spoken*, not time spent
            # deliberately held: otherwise everything deferred overnight expires
            # before the window ends and the deferral is a silent drop.
            if config.QUEUE_ITEM_TTL > 0:
                item.expires_at = detail + config.QUEUE_ITEM_TTL

        displaced, rejected = None, False
        with self._lock:
            if len(self._items) >= config.QUEUE_MAX:
                # Full queue: shed the least important thing rather than refusing
                # the newest. An emergency arriving into a queue of 100 `low`
                # chatter items must not be the one that gets rejected.
                victim = max(self._items, key=lambda i: (i.priority, i.seq))
                if victim.priority > item.priority:
                    self._items.remove(victim)
                    victim.status = "dropped"
                    victim.error = "queue full; displaced by a higher priority utterance"
                    displaced = victim
                else:
                    rejected = True
            if not rejected:
                self._seq += 1
                item.seq = self._seq
                self._items.append(item)
                self._lock.notify_all()
            current = self._current

        # Bookkeeping for the loser happens outside the lock: it writes to SQLite
        # and notifies listeners, neither of which should hold up the queue.
        if displaced is not None:
            self._finalize(displaced, play_skipped=True)
            displaced.done.set()
        if rejected:
            item.status = "dropped"
            item.error = f"queue is full ({config.QUEUE_MAX} items)"
            self._finalize(item, play_skipped=True)
            item.done.set()
            return Submission(False, "dropped", item.error, item)

        if item.not_before:
            item.status = "deferred"
            self._emit("deferred", item.public())
            return Submission(True, "deferred", "held until quiet hours end", item)

        self._maybe_interrupt(current, item)
        self._emit("queued", item.public())
        return Submission(True, "queued", None, item)

    def _maybe_interrupt(self, current, incoming):
        """Cut off what's playing if the new utterance outranks it by enough.

        The gap requirement matters: without it, two `high` alerts would chop
        each other in half and neither would be understood."""
        if not config.INTERRUPT or current is None:
            return
        if current.priority - incoming.priority < config.INTERRUPT_MIN_GAP:
            return
        if self.player.stop():
            log.info("interrupting %s (%s) for %s (%s)", current.id,
                     config.priority_name(current.priority), incoming.id,
                     config.priority_name(incoming.priority))

    # ---- operator actions ---------------------------------------------

    def cancel(self, item_id):
        """Remove a queued item, or stop it if it is the one playing."""
        with self._lock:
            current = self._current
            item = next((i for i in self._items if i.id == item_id), None)
            if item is not None:
                self._items.remove(item)
                item.status = "cancelled"
                self._lock.notify_all()
        if item is not None:
            item.error = "cancelled"
            self._finalize(item, play_skipped=True)
            item.done.set()
            return "cancelled"
        if current is not None and current.id == item_id:
            current.status = "cancelled"
            return "stopped" if self.player.stop() else "not found"
        return "not found"

    def clear(self):
        """Drop everything waiting. Does not touch what is playing — "clear the
        queue" and "stop talking" are different buttons for a reason."""
        with self._lock:
            items, self._items = self._items, []
            self._lock.notify_all()
        for item in items:
            item.status = "cancelled"
            item.error = "queue cleared"
            self._finalize(item, play_skipped=True)
            item.done.set()
        return len(items)

    def skip(self):
        """Stop the current utterance and move on."""
        with self._lock:
            current = self._current
        if current is None:
            return False
        current.status = "cancelled"
        return self.player.stop()

    def snapshot(self):
        with self._lock:
            current = self._current
            items = sorted(self._items, key=lambda i: (i.priority, i.seq))
            waiting = [i.public() for i in items]
        playing = None
        if current is not None:
            playing = current.public()
            playing["playing_for_ms"] = self.player.playing_for_ms()
        return {
            "playing": playing,
            "waiting": waiting,
            "depth": len(waiting),
            "max_depth": config.QUEUE_MAX,
            "muted": self.player.muted,
            "counts": dict(self._counts),
        }

    # ---- the player thread --------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._worker, name="player", daemon=True)
        self._thread.start()
        return self._thread

    def shutdown(self):
        self._shutdown.set()
        with self._lock:
            self._lock.notify_all()
        self.player.stop()

    def _worker(self):
        log.info("player thread started")
        while not self._shutdown.is_set():
            self._expire_stale()
            item = self._take()
            if item is None:
                continue
            with self._lock:
                self._current = item
            try:
                requeued = self._process(item)
            except Exception:
                log.exception("unhandled error speaking %s", item.id)
                item.status = "failed"
                item.fail_stage = "internal"
                item.error = "internal error"
                self._finalize(item, play_skipped=True)
                requeued = False
            finally:
                with self._lock:
                    self._current = None
                    self._lock.notify_all()
            if not requeued:
                item.done.set()
        log.info("player thread stopped")

    def _take(self):
        """Return the next runnable item, or None after a short wait.

        "Runnable" excludes deferred items whose time hasn't come; the wait is
        bounded by the soonest of those so a deferral wakes the thread on time
        without a timer.

        A quiet-hours deferral is a *condition*, not a deadline: the window can
        be snoozed away before the timestamp arrives, and something held for a
        window that is no longer in force should be spoken rather than kept
        until morning. This is the same rule the front of the queue already
        applies in the other direction — what governs is what would actually be
        heard now, not what was true when the utterance arrived.
        """
        with self._lock:
            if self._shutdown.is_set():
                return None
            now = time.time()
            # Not while muted. Releasing a backlog into a muted player would
            # walk it straight into the mute branch and finalize the lot as
            # dropped — turning a snooze into the thing that destroys what it
            # was meant to release. Muted speech that reaches the front on its
            # own schedule is still dropped, as it always was; this only
            # declines to *hasten* that.
            quiet_lifted = not self.quiet.active() and not self.player.muted
            runnable = [i for i in self._items
                        if i.not_before is None or i.not_before <= now
                        or (i.quiet_deferred and quiet_lifted)]
            if runnable:
                item = min(runnable, key=lambda i: (i.priority, i.seq))
                self._items.remove(item)
                if item.quiet_deferred and quiet_lifted:
                    # Released early. Drop the hold so nothing downstream reports
                    # it as still waiting for a window that no longer applies; if
                    # quiet hours resume before it plays, the check at the front
                    # of the queue defers it again.
                    item.not_before = None
                    item.quiet_deferred = False
                item.queue_ms = round((now - item.created_at) * 1000)
                return item
            pending = [i.not_before for i in self._items if i.not_before]
            timeout = TICK_SECONDS
            if pending:
                timeout = max(0.01, min(TICK_SECONDS, min(pending) - now))
            # One bounded wait, then back to the worker loop, which re-checks
            # expiry before asking again. Nothing is lost by returning empty.
            self._lock.wait(timeout)
            return None

    def _expire_stale(self):
        with self._lock:
            now = time.time()
            stale = [i for i in self._items if i.expires_at and i.expires_at <= now]
            for item in stale:
                self._items.remove(item)
        for item in stale:
            item.status = "expired"
            item.error = f"waited longer than QUEUE_ITEM_TTL ({config.QUEUE_ITEM_TTL}s)"
            log.info("expired %s after %.0fs in queue", item.id, now - item.created_at)
            self._finalize(item, play_skipped=True)
            item.done.set()

    def _requeue(self, item, reason):
        """Put an item back for another attempt, behind anything at its level
        that arrived meanwhile."""
        with self._lock:
            self._seq += 1
            item.seq = self._seq
            self._items.append(item)
            self._lock.notify_all()
        log.info("requeued %s (%s)", item.id, reason)

    def _process(self, item):
        """Synthesize and play one utterance. Returns True if it was put back on
        the queue instead of finishing (deferral, interruption)."""
        # Re-check quiet hours: an item can be queued at 21:59 and reach the
        # front at 22:01, and the policy has to apply to when it would be heard.
        action, detail = self.quiet.decide(item.priority)
        if action == "drop":
            item.status = "dropped"
            item.error = detail
            self._finalize(item, play_skipped=True)
            return False
        if action == "attenuate":
            requested = config.VOLUME if item.volume is None else item.volume
            item.volume = min(requested, detail)
        elif action == "defer":
            item.not_before = detail
            item.quiet_deferred = True
            if config.QUEUE_ITEM_TTL > 0:
                item.expires_at = detail + config.QUEUE_ITEM_TTL
            item.status = "deferred"
            # A caller blocked on this can't be held until morning; tell it the
            # utterance is deferred and let it go.
            if item.wait:
                item.wait = False
                item.done.set()
            self._emit("deferred", item.public())
            self._requeue(item, "quiet hours began while queued")
            return True

        if self.player.muted:
            # Muting drops rather than backlogs: nobody wants ten held
            # announcements to fire the instant they unmute.
            item.status = "muted"
            item.error = "playback is muted"
            self._finalize(item, play_skipped=True)
            return False

        item.status = "synthesizing"
        self._emit("started", item.public())

        wav, error, clip_path = self._get_audio(item)
        if wav is None:
            item.status = "failed"
            item.fail_stage = "synthesis"
            item.error = error
            self._finalize(item, play_skipped=True)
            return False

        item.audio_ms = engines.wav_duration_ms(wav)
        item.status = "playing"
        result = self.player.play(wav, sink=item.sink, volume=item.volume, path=clip_path)
        item.play_ms = result.duration_ms

        if result.interrupted:
            item.replays += 1
            if config.INTERRUPT_REQUEUE and item.replays <= MAX_REPLAYS and item.status != "cancelled":
                item.status = "queued"
                self._requeue(item, "interrupted; will be spoken again")
                return True
            item.status = "cancelled" if item.status == "cancelled" else "interrupted"
            item.error = "cut off by a higher-priority utterance"
            self._finalize(item, clip_path=clip_path, wav=wav)
            return False

        if not result.ok:
            item.status = "failed"
            item.fail_stage = "playback"
            item.error = result.error
            self._finalize(item, clip_path=clip_path, wav=wav)
            return False

        item.status = "spoke"
        self._finalize(item, clip_path=clip_path, wav=wav)
        return False

    def _get_audio(self, item):
        """Cache lookup, then synthesis with the engine's fallback chain.

        Returns (wav_bytes, error, path_on_disk). The path lets playback skip
        writing a temp file when the audio came from (or went into) the cache.
        """
        candidates, error = engines.resolve_candidates(item.engine)
        if error:
            return None, error, None

        # The cache is checked per candidate, just before that candidate would be
        # asked to synthesize. Keying on the engine (not just the text) is what
        # makes that safe: a hit is byte-identical to what this engine would have
        # produced, so `random` and `fastest` can reuse whatever earlier picks
        # left behind without ever playing the wrong voice.
        errors = []
        for candidate in candidates:
            cfg = engines.ENGINES[candidate]
            voice = item.voice or cfg["voice"]
            key = cache.cache_key(
                candidate, cfg["model"], voice, item.speed,
                item.lang if candidate == "supertonic" else None,
                item.text, config.LEAD_SILENCE_MS,
            )
            if cache.audio_cache.cacheable(item.text):
                cached, path = cache.audio_cache.get(key)
                if cached:
                    item.cache_hit = True
                    item.engine_used = candidate
                    item.synth_ms = 0
                    log.info("cache hit for %s via %s", item.id, candidate)
                    return cached, None, path

            wav, err, elapsed = engines.synthesize(
                candidate, item.text, item.voice, item.speed, item.lang,
            )
            if wav is None:
                errors.append(err)
                continue
            item.engine_used = candidate
            item.synth_ms = round(elapsed * 1000)
            wav = audio.prepend_silence(wav, config.LEAD_SILENCE_MS)
            path = None
            if cache.audio_cache.cacheable(item.text):
                path = cache.audio_cache.put(key, wav)
            return wav, None, path

        return None, "; ".join(e for e in errors if e), None

    def _finalize(self, item, clip_path=None, wav=None, play_skipped=False):
        """Record the terminal state once, and emit the matching event."""
        self._count(item.status)
        if item.status in ("failed", "dropped", "expired"):
            log.warning("%s %s: %s", item.status, item.id, item.error)
        else:
            log.info("%s %s (%s, %s) in %sms", item.status, item.id,
                     item.engine_used or item.engine,
                     "cached" if item.cache_hit else "synthesized",
                     item.play_ms if item.play_ms is not None else "-")

        # Keep audio for dashboard replay. A cache entry already on disk is
        # referenced in place; anything else is copied only if configured, so
        # the disk cost of history is opt-out.
        stored = clip_path
        if stored is None and wav is not None and not play_skipped:
            stored = history.store_clip(wav)
        item.clip_path = stored

        item.history_id = history.record(
            created_at=item.created_at,
            source=item.source,
            client=item.client,
            text=item.text,
            engine=item.engine,
            engine_used=item.engine_used,
            voice=item.voice,
            speed=item.speed,
            lang=item.lang,
            priority=item.priority,
            sink=item.sink,
            volume=item.volume,
            status=item.status,
            error=item.error,
            queue_ms=item.queue_ms,
            synth_ms=item.synth_ms,
            play_ms=item.play_ms,
            audio_ms=item.audio_ms,
            cache_hit=1 if item.cache_hit else 0,
            clip_path=stored,
        )
        self._emit(item.status, item.public())


queue = SpeechQueue()
