"""Token authentication and rate limiting.

Both are off by default, which keeps the original "trusted LAN, no ceremony"
deployment working unchanged — but the moment `SPEAK_TOKENS` is set, auth turns
itself on rather than needing a second switch. Two things to configure to get
security right is one thing too many.

Tokens are *named* (`SPEAK_TOKENS=laptop=abc,ci=def`) so that history can say
which client spoke, and so one client can be revoked without rotating everyone.

Loopback is exempt by default. A shell on the host is already inside the trust
boundary — it could run `paplay` directly — so requiring a token there only ever
breaks the local `speak.sh` and teaches people to disable auth entirely.

That exemption needs help to mean anything in a container. Published ports are
NAT'd, so a request from a shell on the host arrives from the bridge gateway and
loopback never matches; config adds the detected gateway to the default list to
keep the promise above true. The cost is that anything else on the host sharing
that gateway — another container reaching the published port — is exempt too, so
`AUTH_EXEMPT_CIDRS` set explicitly (or to `none`) overrides it entirely.
"""

import hmac
import ipaddress
import logging
import threading
import time

import config

log = logging.getLogger("auth")

EXEMPT_NETWORKS = config.parse_networks(config.AUTH_EXEMPT_CIDRS)
RATE_LIMIT = config.parse_rate_limit(config.RATE_LIMIT)
RATE_EXEMPT_PRIORITY = config.priority_value(config.RATE_LIMIT_EXEMPT_PRIORITY, -1)


class Identity:
    """Who is calling. `name` is what history records; `authenticated` says
    whether that name came from a token or is just an address."""

    __slots__ = ("name", "address", "authenticated", "exempt")

    def __init__(self, name, address, authenticated=False, exempt=False):
        self.name = name
        self.address = address
        self.authenticated = authenticated
        self.exempt = exempt

    def __str__(self):
        return self.name


def is_exempt(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) is what a dual-stack listener reports
    # for a v4 client; compare on the v4 form so loopback rules still match.
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip in network for network in EXEMPT_NETWORKS)


def extract_token(headers, query):
    """Accept the token from a header or the query string.

    Header is the normal path. `?token=` exists because a browser navigating to
    the dashboard cannot set headers, and because `curl "$URL?token=x"` is what
    people reach for from a shell script. The query form is upgraded to a cookie
    on first use so the token stops appearing in later request URLs.
    """
    auth_header = headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip(), "header"
    direct = headers.get("X-Speak-Token", "").strip()
    if direct:
        return direct, "header"
    cookie = headers.get("Cookie", "")
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "speak_token" and value:
            return value.strip(), "cookie"
    token = (query.get("token") or [None])[0]
    if token:
        return token.strip(), "query"
    return None, None


def identify(headers, query, address):
    """Resolve a request to an Identity, or raise AuthError.

    Returns (identity, token_source) so the caller can decide whether to set the
    dashboard cookie.
    """
    token, source = extract_token(headers, query)

    if token:
        # Compare against every configured token with a constant-time compare, and
        # don't bail on the first match: looping over all of them keeps the time
        # taken independent of *which* token was sent, not just of its contents.
        matched = None
        for name, secret in config.SPEAK_TOKENS.items():
            if hmac.compare_digest(token, secret):
                matched = name
        if matched:
            return Identity(matched, address, authenticated=True), source
        log.warning("rejected an invalid token from %s", address)
        raise AuthError("invalid token")

    if is_exempt(address):
        return Identity(address, address, authenticated=False, exempt=True), None
    if not config.AUTH_REQUIRED:
        return Identity(address, address, authenticated=False), None
    raise AuthError("a token is required: send Authorization: Bearer <token>")


class AuthError(Exception):
    status = 401


class RateLimitError(Exception):
    status = 429

    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


class TokenBucket:
    """Classic token bucket: `count` requests may burst, then they refill at
    count/period.

    A bucket rather than a fixed window because notifiers are bursty by nature —
    a CI run finishing fires four announcements in a second and then nothing for
    an hour. A fixed window would reject the burst; a bucket absorbs it and only
    complains about sustained abuse.
    """

    def __init__(self, count, period):
        self.count = float(count)
        self.period = float(period)
        self.rate = self.count / self.period
        self._lock = threading.Lock()
        self._buckets = {}

    def consume(self, key, amount=1.0):
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.count, now))
            tokens = min(self.count, tokens + (now - last) * self.rate)
            if tokens >= amount:
                self._buckets[key] = (tokens - amount, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            deficit = amount - tokens
            return False, deficit / self.rate

    def peek(self, key):
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.count, now))
            return min(self.count, tokens + (now - last) * self.rate)

    def prune(self, max_idle=3600):
        """Forget buckets that have been full and untouched for a while, so a
        long-lived server doesn't accumulate one entry per address that ever
        probed the port."""
        now = time.monotonic()
        with self._lock:
            for key in [k for k, (_t, last) in self._buckets.items()
                        if now - last > max_idle]:
                del self._buckets[key]

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            return {
                key: round(min(self.count, tokens + (now - last) * self.rate), 2)
                for key, (tokens, last) in self._buckets.items()
            }


limiter = TokenBucket(*RATE_LIMIT) if RATE_LIMIT else None


def check_rate_limit(identity, priority=None):
    """Raise RateLimitError if this identity has spent its allowance.

    Emergencies bypass it. A rate limit that silences an alarm has done more
    damage than the abuse it was protecting against.
    """
    if limiter is None:
        return
    if priority is not None and priority <= RATE_EXEMPT_PRIORITY:
        return
    allowed, retry_after = limiter.consume(identity.name)
    if not allowed:
        retry = max(1, round(retry_after))
        log.warning("rate limited %s (retry in %ss)", identity.name, retry)
        raise RateLimitError(
            f"rate limit is {config.RATE_LIMIT} requests; retry in {retry}s", retry
        )


def status():
    return {
        "auth_required": config.AUTH_REQUIRED,
        "tokens": sorted(config.SPEAK_TOKENS),
        "exempt_networks": [str(n) for n in EXEMPT_NETWORKS],
        "rate_limit": config.RATE_LIMIT if limiter else None,
        "rate_limit_exempt_priority": (
            config.priority_name(RATE_EXEMPT_PRIORITY) if limiter and RATE_EXEMPT_PRIORITY >= 0
            else None
        ),
        "remaining": limiter.snapshot() if limiter else {},
    }
