"""Playback: sink routing, volume, and playback that can be cut short.

One `Player` instance owns the host's audio session for this server. Only the
player thread calls `play()`; `stop()` and the mute flag are called from HTTP
handler threads, which is the whole reason there is a lock in here.

Playback goes through a temp file rather than paplay's stdin. Feeding a pipe
works, but libsndfile can't seek it, so paplay can't know the clip length up
front and terminating mid-clip leaves a writer thread blocked on a broken pipe.
A file sidesteps both, and for cached audio the file already exists so nothing
is written at all.
"""

import io
import logging
import os
import subprocess
import threading
import time
import wave

import config

log = logging.getLogger("audio")

# paplay takes volume on a linear 0-65536 scale; everything user-facing here is
# 0-100 because that is what people expect to type.
PA_VOLUME_MAX = 65536


def prepend_silence(wav_bytes, ms):
    """Return a WAV with `ms` of leading silence. Falls back to the original
    bytes if anything about the WAV can't be parsed — padding is a nicety, so a
    parse failure must never stop playback."""
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


# Sink enumeration shells out to pactl, and /api/status asks for it on every
# dashboard poll — twice. Left uncached that is two processes every couple of
# seconds forever, and on a host where the audio socket is unreachable it is also
# two failures every couple of seconds filling the system log. Hardware doesn't
# change on that timescale, so a few seconds of staleness costs nothing.
_SINK_CACHE_TTL = 10
_sink_cache = {}
_sink_cache_lock = threading.Lock()


def _pactl(args, ttl=_SINK_CACHE_TTL):
    """Run a read-only pactl query, memoized briefly. Returns stdout as text, or
    None if pactl isn't there or can't reach the session — which is informational
    (the dashboard shows fewer details), never fatal."""
    key = tuple(args)
    now = time.monotonic()
    with _sink_cache_lock:
        cached = _sink_cache.get(key)
        if cached and now - cached[1] < ttl:
            return cached[0]
    try:
        result = subprocess.run(
            ["pactl", *args], capture_output=True, timeout=10, check=False,
        )
        output = result.stdout.decode(errors="replace") if result.returncode == 0 else None
        if output is None:
            log.debug("pactl %s failed: %s", " ".join(args),
                      result.stderr.decode(errors="replace").strip()[:200])
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("pactl unavailable: %s", e)
        output = None
    with _sink_cache_lock:
        _sink_cache[key] = (output, now)
    return output


def list_sinks():
    """Available output devices, for the dashboard's routing picker and for
    checking that AUDIO_ROUTES point at something real."""
    output = _pactl(["list", "short", "sinks"])
    if not output:
        return []
    sinks = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            sinks.append({"index": fields[0], "name": fields[1],
                          "state": fields[-1] if len(fields) > 4 else ""})
    return sinks


def default_sink():
    """The session's default sink name, so the dashboard can show what plain
    unrouted playback will actually come out of."""
    output = _pactl(["get-default-sink"])
    return output.strip() or None if output else None


class PlaybackResult:
    """What happened to one clip. `interrupted` is separate from `ok` because an
    interruption is a deliberate outcome — the queue requeues or drops it by
    policy — while `ok=False` means the audio session actually broke."""

    __slots__ = ("ok", "error", "interrupted", "duration_ms", "sink", "volume")

    def __init__(self, ok, error=None, interrupted=False, duration_ms=None,
                 sink=None, volume=None):
        self.ok = ok
        self.error = error
        self.interrupted = interrupted
        self.duration_ms = duration_ms
        self.sink = sink
        self.volume = volume


class Player:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._interrupt_requested = False
        self._muted = False
        self._playing_since = None
        self._tmp_dir = os.path.join(config.DATA_DIR, "tmp")

    # ---- state visible to other threads -------------------------------

    @property
    def muted(self):
        with self._lock:
            return self._muted

    def set_muted(self, value):
        """Muting does not stop what is already playing — that would make the
        dashboard's mute button double as a stop button, and the two are
        different intentions. Callers that want both call stop() too."""
        with self._lock:
            self._muted = bool(value)
        log.info("playback %s", "muted" if value else "unmuted")

    def is_playing(self):
        with self._lock:
            return self._proc is not None

    def playing_for_ms(self):
        with self._lock:
            if self._playing_since is None:
                return None
        return round((time.monotonic() - self._playing_since) * 1000)

    def stop(self):
        """Cut off whatever is playing. Returns True if something was actually
        stopped, so the caller can report "nothing was playing" honestly."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return False
            self._interrupt_requested = True
        try:
            proc.terminate()
        except OSError:
            pass
        return True

    # ---- routing ------------------------------------------------------

    def resolve_sink(self, requested):
        """Route name or raw device name -> device name, or None for the
        session default. Aliases win over raw names so a route can be
        repointed at new hardware without touching any client."""
        name = (requested or "").strip()
        if not name:
            return config.AUDIO_SINK or None
        if name in config.AUDIO_ROUTES:
            return config.AUDIO_ROUTES[name]
        return name

    # ---- playback -----------------------------------------------------

    def play(self, wav_bytes, sink=None, volume=None, path=None):
        """Play one clip, blocking until it finishes or is interrupted.

        `path` is an already-on-disk copy of the same audio (a cache entry);
        when given, nothing is written to a temp file. Only one thread should
        call this at a time — the player thread is the only one that does.
        """
        device = self.resolve_sink(sink)
        level = config.VOLUME if volume is None else volume
        level = max(0, min(100, int(level)))

        if self.muted:
            log.info("muted: discarding %d bytes", len(wav_bytes))
            return PlaybackResult(True, interrupted=False, duration_ms=0,
                                  sink=device, volume=level)

        cmd = ["paplay", "--client-name=speak-server"]
        if device:
            cmd.append(f"--device={device}")
        # --volume goes on *every* clip, including full volume, and that is not
        # redundant. PulseAudio and PipeWire both run module-stream-restore, which
        # remembers a volume per application name and reapplies it to each new
        # stream. Since every clip here is the same client name, one request with
        # `"volume": 15` would otherwise teach the sound server that speak-server
        # plays at 15%, and every later utterance — including ones asking for 100 —
        # would inherit it. Per-request volume has to stay per-request, so the
        # server states the level explicitly and never inherits a remembered one.
        cmd.append(f"--volume={round(level * PA_VOLUME_MAX / 100)}")

        temp_path = None
        if path and os.path.exists(path):
            play_path = path
        else:
            try:
                os.makedirs(self._tmp_dir, exist_ok=True)
                temp_path = os.path.join(
                    self._tmp_dir, f"play-{os.getpid()}-{threading.get_ident()}.wav"
                )
                with open(temp_path, "wb") as fh:
                    fh.write(wav_bytes)
                play_path = temp_path
            except OSError as e:
                # /data unwritable shouldn't mean silence: fall back to feeding
                # paplay over stdin, which needs no filesystem at all.
                log.warning("cannot write temp audio (%s); using stdin", e)
                return self._play_via_stdin(cmd, wav_bytes, device, level)

        cmd.append(play_path)
        try:
            return self._run(cmd, device, level)
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _run(self, cmd, device, level):
        started = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as e:
            return PlaybackResult(False, f"cannot run paplay: {e}", sink=device, volume=level)

        with self._lock:
            self._proc = proc
            self._interrupt_requested = False
            self._playing_since = started

        try:
            try:
                _, stderr = proc.communicate(timeout=config.PLAY_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate()
                return PlaybackResult(
                    False, f"paplay exceeded PLAY_TIMEOUT ({config.PLAY_TIMEOUT}s)",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    sink=device, volume=level,
                )
        finally:
            with self._lock:
                interrupted = self._interrupt_requested
                self._proc = None
                self._interrupt_requested = False
                self._playing_since = None

        duration_ms = round((time.monotonic() - started) * 1000)
        if interrupted:
            # terminate() makes paplay exit non-zero; that is us, not a fault.
            return PlaybackResult(True, interrupted=True, duration_ms=duration_ms,
                                  sink=device, volume=level)
        if proc.returncode != 0:
            detail = (stderr or b"")[:300].decode(errors="replace").strip()
            return PlaybackResult(
                False, f"paplay failed ({proc.returncode}): {detail}",
                duration_ms=duration_ms, sink=device, volume=level,
            )
        return PlaybackResult(True, duration_ms=duration_ms, sink=device, volume=level)

    def _play_via_stdin(self, cmd, wav_bytes, device, level):
        """Last-resort path when no temp file can be written. Not interruptible
        in the middle of the pipe write, which is why it isn't the default."""
        cmd = cmd + ["/dev/stdin"]
        started = time.monotonic()
        try:
            result = subprocess.run(
                cmd, input=wav_bytes, capture_output=True,
                timeout=config.PLAY_TIMEOUT, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return PlaybackResult(False, f"paplay failed: {e}", sink=device, volume=level)
        duration_ms = round((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            detail = result.stderr[:300].decode(errors="replace").strip()
            return PlaybackResult(False, f"paplay failed ({result.returncode}): {detail}",
                                  duration_ms=duration_ms, sink=device, volume=level)
        return PlaybackResult(True, duration_ms=duration_ms, sink=device, volume=level)

    def cleanup_temp(self):
        """Remove temp clips left by a previous process that was killed
        mid-playback. Called once at startup; nothing else writes there."""
        try:
            for name in os.listdir(self._tmp_dir):
                if name.startswith("play-") and name.endswith(".wav"):
                    try:
                        os.unlink(os.path.join(self._tmp_dir, name))
                    except OSError:
                        pass
        except OSError:
            pass


player = Player()
