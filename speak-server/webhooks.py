"""Webhook receivers: turn somebody else's JSON into a spoken sentence.

`POST /speak` already accepts JSON, but the things you actually want to hear
from — GitHub, Alertmanager, Grafana, Uptime Kuma, a home automation hub — send
*their* schema and can't be talked out of it. A receiver is therefore a named
endpoint plus a template that pulls fields out of whatever arrives.

Receivers are defined in a JSON file rather than environment variables. A
template with braces and dotted paths in a compose `environment:` block is
miserable to write and worse to read, and there is usually more than one.

    {
      "ci": {
        "preset": "github",
        "priority": "low",
        "secret": "put-this-in-the-github-webhook-url"
      },
      "alerts": {
        "preset": "alertmanager",
        "priority": "high",
        "match": {"status": "firing"}
      },
      "hass": {
        "template": "{message}",
        "voice": "af_bella",
        "sink": "kitchen"
      }
    }

Each receiver is reachable at `POST /webhook/<name>`.
"""

import json
import logging
import os
import re

import config
from queues import ValidationError, build_utterance

log = logging.getLogger("webhooks")

# Field names to try when a receiver has no template. Ordered from most to least
# specific, so a payload carrying both `title` and `message` speaks the message.
GENERIC_FIELDS = (
    "text", "message", "msg", "body", "summary", "description", "title",
    "alert", "content", "status",
)

PRESETS = {
    # GitHub sends `action` plus one of a dozen object shapes; the pull_request
    # and issue cases are what people actually wire up to a speaker.
    "github": "{repository.name}: {sender.login} {action} "
              "{pull_request.title}{issue.title}{release.tag_name}",
    # Alertmanager batches alerts; speaking the first one's summary is right,
    # because hearing "and 6 others" is more useful than hearing all seven.
    "alertmanager": "{status}: {alerts.0.labels.alertname} on "
                    "{alerts.0.labels.instance} — {alerts.0.annotations.summary}",
    "grafana": "{title}: {message}",
    "uptimekuma": "{monitor.name} is {heartbeat.status}: {msg}",
    "generic": None,  # falls through to GENERIC_FIELDS
}

# {a.b.0.c} — dotted path, digits index into lists. Braces are doubled to escape.
_FIELD = re.compile(r"\{([A-Za-z0-9_.\-]+)\}")

# Punctuation that only exists to join clauses, so it should disappear along with
# a clause that rendered empty. Plain hyphen is deliberately absent: it carries
# meaning far too often (negative numbers, hyphenated words, ISO dates) to strip.
_JOINERS = "—–:,;·"
_ADJACENT_JOINERS = re.compile(f"([{_JOINERS}])(\\s*[{_JOINERS}])+")


def lookup(payload, path):
    """Follow a dotted path into nested dicts and lists. Returns "" for anything
    missing, because a template that mentions a field the payload happens not to
    have should lose that clause, not fail the whole request."""
    value = payload
    for part in path.split("."):
        if isinstance(value, dict):
            if part not in value:
                return ""
            value = value[part]
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return ""
        else:
            return ""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        # A whole object read aloud is noise; say nothing rather than JSON.
        return ""
    return str(value)


def render(template, payload):
    """Substitute {paths} from the payload, then tidy up what's left.

    Templates are written for the general case, so clauses routinely come out
    empty (a GitHub `push` has no `pull_request.title`). Collapsing the leftover
    whitespace and dangling punctuation is what makes one template usable across
    several event types instead of needing one per event.
    """
    out = _FIELD.sub(lambda m: lookup(payload, m.group(1)), template)
    out = out.replace("{{", "{").replace("}}", "}")
    out = re.sub(r"\s+", " ", out).strip()
    # An empty clause strands its joiners. Two can end up side by side ("repo: —
    # summary" when the middle field was missing), so collapse those to one, then
    # trim any left dangling at either end. Joiners *between* two real clauses are
    # doing their job and must survive — which is why this doesn't just delete
    # every joiner surrounded by whitespace.
    out = _ADJACENT_JOINERS.sub(r"\1", out)
    out = out.strip(f" {_JOINERS}")
    return re.sub(r"\s+", " ", out).strip()


class Receiver:
    def __init__(self, name, spec):
        self.name = name
        self.template = spec.get("template")
        preset = spec.get("preset", "generic")
        if self.template is None:
            if preset not in PRESETS:
                raise ValueError(f"unknown preset {preset!r}; one of: {', '.join(PRESETS)}")
            self.template = PRESETS[preset]
        self.preset = preset
        self.secret = spec.get("secret")
        self.match = spec.get("match") or {}
        self.priority = spec.get("priority", config.DEFAULT_PRIORITY)
        self.engine = spec.get("engine")
        self.voice = spec.get("voice")
        self.model = spec.get("model")
        self.speed = spec.get("speed")
        self.lang = spec.get("lang")
        self.sink = spec.get("sink")
        self.volume = spec.get("volume")
        self.prefix = spec.get("prefix", "")
        # Webhook payloads are machine-generated and can be enormous; unlike text
        # a person typed, truncating is kinder than refusing. 0 disables.
        self.max_length = int(spec.get("max_length", 300))
        # Webhooks are fire-and-forget by nature: the sender wants a fast 2xx and
        # has no interest in waiting for audio to finish.
        self.wait = bool(spec.get("wait", False))

        if config.priority_value(self.priority) is None:
            raise ValueError(f"unknown priority {self.priority!r}")

    def matches(self, payload):
        """Optional filter, so `match: {"status": "firing"}` means resolved
        alerts don't wake anyone up. Values compare as strings — webhook senders
        are inconsistent about quoting numbers."""
        for path, expected in self.match.items():
            actual = lookup(payload, path)
            if isinstance(expected, list):
                if actual not in [str(e) for e in expected]:
                    return False
            elif actual != str(expected):
                return False
        return True

    def speech_for(self, payload):
        """The sentence to speak, or "" if this payload has nothing to say."""
        if self.template:
            text = render(self.template, payload)
        elif isinstance(payload, str):
            text = payload.strip()
        else:
            text = ""
            for field in GENERIC_FIELDS:
                candidate = lookup(payload, field)
                if candidate.strip():
                    text = candidate.strip()
                    break
        if self.prefix and text:
            text = f"{self.prefix} {text}"
        if self.max_length > 0 and len(text) > self.max_length:
            # Cut at a word boundary; a sentence that stops mid-word sounds like
            # a fault rather than a summary.
            text = text[: self.max_length].rsplit(" ", 1)[0]
        return text

    def build(self, payload, client):
        text = self.speech_for(payload)
        if not text:
            raise ValidationError(
                f"webhook {self.name!r} found nothing to say in this payload "
                f"(template: {self.template or 'generic field search'})", 422
            )
        body = {"text": text, "priority": self.priority, "wait": self.wait}
        for key in ("engine", "voice", "model", "speed", "lang", "sink", "volume"):
            value = getattr(self, key)
            if value is not None:
                body[key] = value
        return build_utterance(body, source=f"webhook:{self.name}", client=client)

    def public(self):
        return {
            "name": self.name,
            "preset": self.preset,
            "template": self.template,
            "priority": self.priority,
            "engine": self.engine,
            "voice": self.voice,
            "sink": self.sink,
            "match": self.match,
            "requires_secret": bool(self.secret),
            "path": f"/webhook/{self.name}",
        }


class Registry:
    def __init__(self, path=None):
        self.path = path or config.WEBHOOKS_FILE
        self.receivers = {}
        self.error = None
        self.load()

    def load(self):
        """Read the receiver file. A missing file is normal (no receivers
        configured); a malformed one is logged and leaves the registry empty
        rather than stopping the server, because speech through the other front
        ends still works."""
        self.receivers, self.error = {}, None
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                spec = json.load(fh)
        except (OSError, ValueError) as e:
            self.error = f"cannot read {self.path}: {e}"
            log.warning("%s", self.error)
            return
        if not isinstance(spec, dict):
            self.error = f"{self.path} must contain a JSON object of receivers"
            log.warning("%s", self.error)
            return
        for name, entry in spec.items():
            # JSON has no comments, so a leading underscore is the convention for
            # one. Skipping it silently keeps a documented example file from
            # logging a warning every time the server starts.
            if name.startswith("_"):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                log.warning("webhook %r: name must be alphanumeric/dash/underscore", name)
                continue
            if not isinstance(entry, dict):
                log.warning("webhook %r: definition must be an object", name)
                continue
            try:
                self.receivers[name] = Receiver(name, entry)
            except (ValueError, TypeError) as e:
                log.warning("webhook %r: %s", name, e)
        if self.receivers:
            log.info("loaded %d webhook receiver(s): %s",
                     len(self.receivers), ", ".join(sorted(self.receivers)))

    def get(self, name):
        return self.receivers.get(name)

    def public(self):
        return {
            "file": self.path,
            "error": self.error,
            "receivers": [r.public() for r in self.receivers.values()],
        }


registry = Registry()
