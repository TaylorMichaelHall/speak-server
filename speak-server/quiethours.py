"""Quiet hours: time windows in which unimportant speech is held back.

A window is `HH:MM-HH:MM` in a configured timezone, and may wrap past midnight
(`22:00-08:00`) because that is the case people actually want. Several windows
may be listed.

Policy is separate from the window, because "don't talk at night" means
different things: hold it until morning (`defer`), throw it away (`drop`), or say
it quietly now (`attenuate`). Anything at or above the override priority ignores
the window entirely — a quiet-hours setting that suppresses a smoke alarm is a
bug, not a feature.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

import config

log = logging.getLogger("quiet")

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - stdlib since 3.9
    ZoneInfo = None


def _load_tz(name):
    """Fall back to local time rather than failing to start: a container without
    tzdata still needs to speak, and UTC windows are at least predictable."""
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception as e:  # ZoneInfoNotFoundError and friends
        log.warning("unknown timezone %r (%s); using container local time", name, e)
        return None


def parse_windows(spec):
    """'22:00-08:00,13:00-13:30' -> [((22,0),(8,0)), ((13,0),(13,30))].
    Unparseable entries are dropped with a warning rather than taken as
    midnight-to-midnight, which would silence the server completely."""
    windows = []
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        start_s, sep, end_s = entry.partition("-")
        if not sep:
            log.warning("QUIET_HOURS: ignoring %r (expected HH:MM-HH:MM)", entry)
            continue
        try:
            start = _parse_hhmm(start_s)
            end = _parse_hhmm(end_s)
        except ValueError as e:
            log.warning("QUIET_HOURS: ignoring %r (%s)", entry, e)
            continue
        if start == end:
            log.warning("QUIET_HOURS: ignoring %r (zero-length window)", entry)
            continue
        windows.append((start, end))
    return windows


def _parse_hhmm(text):
    hour_s, sep, minute_s = text.strip().partition(":")
    if not sep:
        raise ValueError("missing ':'")
    hour, minute = int(hour_s), int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("out of range")
    return hour, minute


class QuietHours:
    def __init__(self, spec=None, tz_name=None, policy=None, override=None,
                 volume=None):
        self.windows = parse_windows(config.QUIET_HOURS if spec is None else spec)
        self.tz = _load_tz(config.QUIET_HOURS_TZ if tz_name is None else tz_name)
        self.tz_name = config.QUIET_HOURS_TZ if tz_name is None else tz_name
        self.policy = config.QUIET_HOURS_POLICY if policy is None else policy
        self.override_priority = config.priority_value(
            config.QUIET_HOURS_OVERRIDE if override is None else override, 0
        )
        self.volume = config.QUIET_HOURS_VOLUME if volume is None else volume
        self._lock = threading.Lock()
        # Operator escape hatch: suppress quiet hours until a timestamp (or
        # forever). Watching a film at 23:00 shouldn't require an edit-and-restart.
        self._snooze_until = None
        if self.windows:
            log.info("quiet hours %s (%s), policy=%s, override>=%s",
                     config.QUIET_HOURS or spec, self.tz_name, self.policy,
                     config.priority_name(self.override_priority))

    def _now(self):
        return datetime.now(self.tz) if self.tz else datetime.now()

    def snoozed(self):
        with self._lock:
            until = self._snooze_until
        if until is None:
            return False
        if until == "forever":
            return True
        if time.time() < until:
            return True
        with self._lock:
            # Expired — clear it so status stops reporting a snooze.
            if self._snooze_until == until:
                self._snooze_until = None
        return False

    def snooze(self, seconds=None):
        with self._lock:
            self._snooze_until = "forever" if not seconds else time.time() + float(seconds)
        log.info("quiet hours snoozed (%s)", seconds or "indefinitely")

    def unsnooze(self):
        with self._lock:
            self._snooze_until = None

    def active(self, now=None):
        """Whether a quiet window covers `now`."""
        if not self.windows or self.snoozed():
            return False
        now = now or self._now()
        minutes = now.hour * 60 + now.minute
        for (sh, sm), (eh, em) in self.windows:
            start, end = sh * 60 + sm, eh * 60 + em
            if start < end:
                if start <= minutes < end:
                    return True
            # Wrapping window: inside it if we're after the start OR before the
            # end, which are the two halves either side of midnight.
            elif minutes >= start or minutes < end:
                return True
        return False

    def ends_at(self, now=None):
        """Wall-clock timestamp when the current window ends, for deferral.
        None when no window is active.

        Overlapping and adjacent windows are followed through: deferring to the
        end of the first window would just re-defer a moment later, so walk
        forward until a genuinely quiet-free minute is found."""
        now = now or self._now()
        if not self.active(now):
            return None
        probe = now.replace(second=0, microsecond=0)
        # 24h + 1 minute of probing covers any combination of windows; if every
        # minute of the day is quiet, give up and let it speak rather than
        # deferring forever.
        for _ in range(24 * 60 + 1):
            probe += timedelta(minutes=1)
            if not self.active(probe):
                return probe.timestamp()
        log.warning("quiet hours cover the entire day; ignoring the window")
        return None

    def decide(self, priority):
        """What to do with an utterance of this priority, right now.

        Returns (action, detail): ('allow', None), ('drop', reason),
        ('defer', timestamp) or ('attenuate', volume).
        """
        if not self.active():
            return "allow", None
        if priority <= self.override_priority:
            return "allow", None
        if self.policy == "drop":
            return "drop", f"quiet hours ({self.tz_name}); policy=drop"
        if self.policy == "attenuate":
            return "attenuate", self.volume
        ends = self.ends_at()
        if ends is None:
            return "allow", None
        return "defer", ends

    def status(self):
        with self._lock:
            snooze_until = self._snooze_until
        now = self._now()
        return {
            "configured": bool(self.windows),
            "windows": [f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
                        for (sh, sm), (eh, em) in self.windows],
            "timezone": self.tz_name,
            "local_time": now.strftime("%H:%M"),
            "policy": self.policy,
            "override_priority": config.priority_name(self.override_priority),
            "attenuate_volume": self.volume,
            "active": self.active(now),
            "ends_at": self.ends_at(now),
            "snoozed": self.snoozed(),
            "snooze_until": None if snooze_until in (None, "forever") else snooze_until,
        }


quiet_hours = QuietHours()
