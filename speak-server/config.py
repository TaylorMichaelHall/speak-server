"""Environment parsing for speak-server.

Everything is configured by environment variable, and compose always *passes*
every variable it knows about — an unset one arrives as the empty string
rather than being absent. So the rule throughout is: empty means "not
configured", and the default applies. Reading `os.environ.get(k, default)`
directly would break that, which is why nothing outside this module does.

All values are read once at import. The server is restarted to reconfigure it;
the only things that change at runtime (mute, quiet-hours override) live in the
modules that own them, not here.
"""

import ipaddress
import logging
import os
import sys

# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


def env_str(name, default=""):
    value = os.environ.get(name, "")
    return value.strip() if value.strip() else default


def env_int(name, default):
    raw = env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


def env_float(name, default):
    raw = env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logging.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def env_bool(name, default):
    raw = env_str(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    logging.warning("%s=%r is not a boolean; using %s", name, raw, default)
    return default


def env_list(name, default=()):
    """Comma-separated list, empty entries dropped."""
    raw = env_str(name)
    if not raw:
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def in_container():
    """Whether we're running inside a container, and so behind a NAT that
    rewrites the source address of host-local callers."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def parse_default_gateway(routes):
    """Pull the default gateway out of /proc/net/route's contents.

    Split from the file read so it can be tested without a container: the
    format is fixed-width columns, and the gateway is little-endian hex
    because the kernel prints the raw in-memory address.
    """
    for line in routes.splitlines()[1:]:
        fields = line.split()
        # Destination 0.0.0.0 marks the default route. A zero gateway there is
        # a directly-attached default (no next hop), which is not an address.
        if len(fields) > 2 and fields[1] == "00000000" and fields[2] != "00000000":
            try:
                packed = int(fields[2], 16).to_bytes(4, "little")
            except (ValueError, OverflowError):
                return None
            return str(ipaddress.IPv4Address(packed))
    return None


def default_gateway(path="/proc/net/route"):
    try:
        with open(path) as handle:
            return parse_default_gateway(handle.read())
    except OSError:
        return None


def env_pairs(name, sep="="):
    """`a=1,b=2` -> {"a": "1", "b": "2"}. Used for token and route maps, where
    the value may itself contain the separator (tokens can), so only the first
    one splits."""
    out = {}
    for entry in env_list(name):
        key, found, value = entry.partition(sep)
        if not found:
            logging.warning("%s: ignoring %r (expected key%svalue)", name, entry, sep)
            continue
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

LOG_LEVEL = env_str("LOG_LEVEL", "info").upper()


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

PORT = env_int("PORT", 8899)
BIND = env_str("BIND", "0.0.0.0")
# Where the cache, history DB and any runtime state live. A single directory so
# the compose file needs exactly one volume.
DATA_DIR = env_str("DATA_DIR", "/data")

# --------------------------------------------------------------------------
# engines
# --------------------------------------------------------------------------

KOKORO_URL = env_str("KOKORO_URL", "http://kokoro:8880")
SUPERTONIC_URL = env_str("SUPERTONIC_URL", "http://supertonic:7788")
DEFAULT_ENGINE = env_str("ENGINE", "kokoro")
DEFAULT_VOICE = env_str("VOICE", "af_heart")
SUPERTONIC_VOICE = env_str("SUPERTONIC_VOICE", "M1")

SYNTH_TIMEOUT = env_int("SYNTH_TIMEOUT", 120)
# How many recent syntheses feed the per-engine latency estimate used by
# ENGINE=fastest. Small enough to react to an engine going slow, large enough
# that one outlier doesn't flip the choice.
LATENCY_WINDOW = env_int("LATENCY_WINDOW", 10)
# After this many consecutive failures an engine is skipped by `fastest` and
# `random` until its cooldown expires. Named engines are always still tried —
# the caller asked for that one and deserves the real error.
ENGINE_FAILURE_THRESHOLD = env_int("ENGINE_FAILURE_THRESHOLD", 3)
ENGINE_COOLDOWN = env_int("ENGINE_COOLDOWN", 60)

# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------

# The sink audio plays on when a request doesn't name one. Empty means the
# Pulse/PipeWire default sink, which is what most setups want.
AUDIO_SINK = env_str("AUDIO_SINK")
# Friendly names for sinks, so clients say `"sink": "desk"` instead of pasting
# a 60-character PulseAudio device name that changes when hardware moves.
# AUDIO_ROUTES=desk=alsa_output.pci-0000_00_1f.3.analog-stereo,tv=alsa_output.hdmi
AUDIO_ROUTES = env_pairs("AUDIO_ROUTES")
# 0-100, mapped to paplay's 0-65536 scale. Applies unless a request overrides.
VOLUME = env_int("VOLUME", 100)
PLAY_TIMEOUT = env_int("PLAY_TIMEOUT", 600)
# The audio sink suspends when idle; opening a stream spends the first few
# hundred ms resuming, which clips the start of speech. Prepend silence so the
# resume ramp eats that instead of the first syllable. Set 0 to disable.
LEAD_SILENCE_MS = env_int("LEAD_SILENCE_MS", 500)

# --------------------------------------------------------------------------
# queue, priority, quiet hours
# --------------------------------------------------------------------------

# Lower number = more important. Names are what clients send; the numbers only
# ever appear in comparisons.
PRIORITIES = {"emergency": 0, "high": 1, "normal": 2, "low": 3}
DEFAULT_PRIORITY = env_str("DEFAULT_PRIORITY", "normal")
QUEUE_MAX = env_int("QUEUE_MAX", 100)
# Longer text is refused rather than truncated: half a sentence spoken aloud is
# worse than a clear error, and it caps what one request can cost in synthesis.
MAX_TEXT = env_int("MAX_TEXT", 5000)
SPEED_MIN = env_float("SPEED_MIN", 0.25)
SPEED_MAX = env_float("SPEED_MAX", 4.0)
# An announcement that waited this long is usually stale — "build started" is
# noise once the build has finished. 0 disables expiry.
QUEUE_ITEM_TTL = env_int("QUEUE_ITEM_TTL", 300)
# Whether a higher-priority item may cut off something already playing.
INTERRUPT = env_bool("INTERRUPT", True)
# Interrupting only matters if the gap is real: with 1, `high` interrupts
# `normal`. Raise it to make interruption rarer.
INTERRUPT_MIN_GAP = env_int("INTERRUPT_MIN_GAP", 1)
# Requeue what was interrupted so it still gets said, just later. Off means the
# interrupted utterance is dropped, which suits alert-style use.
INTERRUPT_REQUEUE = env_bool("INTERRUPT_REQUEUE", False)

# "22:00-08:00" (wrapping past midnight is fine), or empty for none. Multiple
# windows may be comma-separated.
QUIET_HOURS = env_str("QUIET_HOURS")
QUIET_HOURS_TZ = env_str("QUIET_HOURS_TZ", env_str("TZ", "UTC"))
# What happens to an utterance that arrives during quiet hours and isn't
# important enough to override: defer (speak when the window ends), drop, or
# attenuate (speak now, quietly).
QUIET_HOURS_POLICY = env_str("QUIET_HOURS_POLICY", "defer")
# Priority at or above which quiet hours are ignored entirely.
QUIET_HOURS_OVERRIDE = env_str("QUIET_HOURS_OVERRIDE", "emergency")
# Volume (0-100) used by the "attenuate" policy.
#
# 60, not the 25 you might reach for: the sound server's volume scale is cubic
# (dB = 60*log10(level/100)), the same curve as a desktop volume slider. 25 is
# -36 dB, which is inaudible rather than soft — it would make "attenuate"
# indistinguishable from "drop". 60 is about -13 dB: clearly quieter, still heard.
QUIET_HOURS_VOLUME = env_int("QUIET_HOURS_VOLUME", 60)

# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

CACHE_ENABLED = env_bool("CACHE_ENABLED", True)
CACHE_DIR = env_str("CACHE_DIR", os.path.join(DATA_DIR, "cache"))
CACHE_MAX_MB = env_int("CACHE_MAX_MB", 512)
CACHE_MAX_AGE_DAYS = env_int("CACHE_MAX_AGE_DAYS", 30)
# Long one-off text (a paragraph of build output) pollutes the cache without
# ever being asked for twice; short repeated phrases are the whole point.
CACHE_MAX_TEXT = env_int("CACHE_MAX_TEXT", 400)
VOICE_LIST_TTL = env_int("VOICE_LIST_TTL", 300)

# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------

HISTORY_ENABLED = env_bool("HISTORY_ENABLED", True)
HISTORY_DB = env_str("HISTORY_DB", os.path.join(DATA_DIR, "history.db"))
HISTORY_MAX_ROWS = env_int("HISTORY_MAX_ROWS", 5000)
HISTORY_MAX_AGE_DAYS = env_int("HISTORY_MAX_AGE_DAYS", 30)
# Keep a copy of the audio for dashboard replay even when the cache wouldn't
# have kept it (text too long, cache off). Costs disk; bounded by the same
# retention as rows.
HISTORY_KEEP_AUDIO = env_bool("HISTORY_KEEP_AUDIO", True)
HISTORY_AUDIO_DIR = env_str("HISTORY_AUDIO_DIR", os.path.join(DATA_DIR, "clips"))
HISTORY_AUDIO_MAX_MB = env_int("HISTORY_AUDIO_MAX_MB", 256)

# --------------------------------------------------------------------------
# auth and rate limits
# --------------------------------------------------------------------------

# SPEAK_TOKENS=laptop=s3cret,ci=other  — named so the dashboard and history can
# say *who* spoke, and so one client's token can be revoked alone.
SPEAK_TOKENS = env_pairs("SPEAK_TOKENS")
_single = env_str("SPEAK_TOKEN")
if _single:
    SPEAK_TOKENS.setdefault("default", _single)
# Auth turns itself on when tokens exist. Setting this true with no tokens is a
# misconfiguration the server refuses to start with, rather than silently
# locking every client out.
AUTH_REQUIRED = env_bool("AUTH_REQUIRED", bool(SPEAK_TOKENS))
# Callers from these networks skip auth. Defaults to loopback so a shell on the
# host keeps working after tokens are turned on.
#
# "Empty means unset" applies here as everywhere, so an empty value can't mean
# "no exemptions" — it means "use the default". Requiring a token even from
# loopback therefore needs a word: AUTH_EXEMPT_CIDRS=none.
AUTH_EXEMPT_CIDRS = env_list("AUTH_EXEMPT_CIDRS", ["127.0.0.0/8", "::1/128"])
_exempt_was_configured = bool(env_str("AUTH_EXEMPT_CIDRS"))
# The address the container will actually see for host-local callers, when that
# isn't loopback. None unless the gateway had to be added below.
AUTH_EXEMPT_GATEWAY = None
if len(AUTH_EXEMPT_CIDRS) == 1 and AUTH_EXEMPT_CIDRS[0].lower() in ("none", "off"):
    AUTH_EXEMPT_CIDRS = []
elif not _exempt_was_configured:
    # Loopback alone is a promise this deployment cannot keep. Published ports
    # are NAT'd: a request from a shell on the host arrives from the bridge
    # gateway, so the container never sees 127.0.0.1 and the exemption never
    # fires. The local speak.sh then gets a 401 the moment tokens are set —
    # precisely the breakage the exemption exists to prevent, and the one that
    # teaches people to turn auth off again.
    #
    # Only when the operator hasn't stated their own list: an explicit
    # AUTH_EXEMPT_CIDRS is honoured exactly as written.
    _gateway = default_gateway() if in_container() else None
    if _gateway:
        AUTH_EXEMPT_CIDRS = AUTH_EXEMPT_CIDRS + [f"{_gateway}/32"]
        AUTH_EXEMPT_GATEWAY = _gateway
# The dashboard is a read/write control surface; it follows the same auth as
# everything else, but browsers can't send an Authorization header on a plain
# navigation, so it accepts ?token= and sets a cookie.
DASHBOARD_ENABLED = env_bool("DASHBOARD", True)

# "requests/seconds" — a token bucket, so a burst up to the full count is
# allowed and then it refills. Empty disables rate limiting.
RATE_LIMIT = env_str("RATE_LIMIT", "60/60")
# Emergencies bypass the limit: a rate limit that silences an alarm is worse
# than the abuse it prevents.
RATE_LIMIT_EXEMPT_PRIORITY = env_str("RATE_LIMIT_EXEMPT_PRIORITY", "emergency")

# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------

MQTT_HOST = env_str("MQTT_HOST")
MQTT_PORT = env_int("MQTT_PORT", 1883)
MQTT_USERNAME = env_str("MQTT_USERNAME")
MQTT_PASSWORD = env_str("MQTT_PASSWORD")
MQTT_TLS = env_bool("MQTT_TLS", False)
MQTT_CLIENT_ID = env_str("MQTT_CLIENT_ID", "speak-server")
# Subscribed. `speak/say` takes plain text or the same JSON body as /speak;
# `speak/say/<priority>` sets the priority from the topic, which is easier from
# constrained publishers.
MQTT_TOPIC = env_str("MQTT_TOPIC", "speak/say")
# Retained snapshot of what the server is doing, plus per-utterance events.
MQTT_STATUS_TOPIC = env_str("MQTT_STATUS_TOPIC", "speak/status")
MQTT_EVENT_TOPIC = env_str("MQTT_EVENT_TOPIC", "speak/event")
MQTT_QOS = env_int("MQTT_QOS", 1)

# --------------------------------------------------------------------------
# webhooks
# --------------------------------------------------------------------------

# JSON file defining POST /webhook/<name> receivers. Absent means no receivers,
# which is the safe default for an endpoint that can't send a token.
WEBHOOKS_FILE = env_str("WEBHOOKS_FILE", os.path.join(DATA_DIR, "webhooks.json"))


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def priority_value(name, default=None):
    """Priority name -> comparable number. Accepts a bare integer too, so a
    caller can be finer-grained than the four names if it wants."""
    if isinstance(name, (int, float)) and not isinstance(name, bool):
        return int(name)
    key = str(name).strip().lower()
    if key in PRIORITIES:
        return PRIORITIES[key]
    try:
        return int(key)
    except ValueError:
        return default


def priority_name(value):
    """Number -> the closest name, for display. Numbers between the named
    levels report as the next-lower name so the dashboard never shows a blank."""
    for name, number in sorted(PRIORITIES.items(), key=lambda kv: kv[1]):
        if value <= number:
            return name
    return "low"


def parse_rate_limit(spec):
    """'60/60' -> (60.0, 60.0). Returns None when disabled or unparseable."""
    if not spec:
        return None
    count, _, period = spec.partition("/")
    try:
        count, period = float(count), float(period or 60)
    except ValueError:
        logging.warning("RATE_LIMIT=%r is not count/seconds; disabling", spec)
        return None
    if count <= 0 or period <= 0:
        return None
    return count, period


def parse_networks(cidrs):
    out = []
    for entry in cidrs:
        try:
            out.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logging.warning("AUTH_EXEMPT_CIDRS: ignoring invalid network %r", entry)
    return out


def validate():
    """Fail fast on settings that would otherwise misbehave quietly hours
    later. Returns a list of fatal errors; warnings are logged in place."""
    fatal = []

    if priority_value(DEFAULT_PRIORITY) is None:
        fatal.append(f"DEFAULT_PRIORITY={DEFAULT_PRIORITY!r} is not one of {list(PRIORITIES)}")
    if QUIET_HOURS_POLICY not in ("defer", "drop", "attenuate"):
        fatal.append(
            f"QUIET_HOURS_POLICY={QUIET_HOURS_POLICY!r} must be defer, drop or attenuate"
        )
    if AUTH_REQUIRED and not SPEAK_TOKENS:
        fatal.append("AUTH_REQUIRED is set but no SPEAK_TOKEN/SPEAK_TOKENS were given")
    if not 0 <= VOLUME <= 100:
        fatal.append(f"VOLUME={VOLUME} must be 0-100")
    if not 0 <= QUIET_HOURS_VOLUME <= 100:
        fatal.append(f"QUIET_HOURS_VOLUME={QUIET_HOURS_VOLUME} must be 0-100")

    if not AUTH_REQUIRED:
        logging.warning(
            "no tokens configured: anyone who can reach port %d can make this "
            "machine talk (set SPEAK_TOKENS to change that)",
            PORT,
        )
    return fatal
