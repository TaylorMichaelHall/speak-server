"""speak-server entrypoint: POST text -> synthesize -> play on host speakers.

Runs in a container with the host's Pulse socket mounted. Playback is serialized
by a single player thread, so overlapping requests queue (by priority) instead of
talking over each other. Errors map to non-2xx so remote callers never believe
they spoke when nothing played.

This module does nothing but wire the pieces together and keep the process alive:

    config      environment parsing, one place, empty means unset
    engines     the TTS backends, health and measured latency
    cache       content-addressed audio cache
    audio       sink routing, volume, interruptible playback
    quiethours  time windows and what to do inside them
    queues      priority queue and the player thread
    history     SQLite log of what was said, and the audio to replay it
    auth        tokens and rate limits
    api         HTTP surface and the dashboard
    webhooks    templated receivers for other people's JSON
    mqtt        optional broker bridge
"""

import logging
import os
import signal
import sys
import threading

import config

config.setup_logging()
log = logging.getLogger("server")

import api          # noqa: E402  - logging must be configured before these import
import audio        # noqa: E402
import cache        # noqa: E402
import engines      # noqa: E402
import mqtt         # noqa: E402
import webhooks     # noqa: E402
from history import history      # noqa: E402
from queues import queue        # noqa: E402
from quiethours import quiet_hours  # noqa: E402

# Retention and bucket pruning run on a timer rather than inline: they walk
# directories and delete rows, neither of which belongs in the path of something
# a person is waiting to hear.
MAINTENANCE_INTERVAL = 900


def maintenance_loop(stop_event):
    """Housekeeping for a process expected to run for months unattended: keep the
    cache and history inside their limits, and forget rate-limit buckets for
    clients that have gone away."""
    import auth

    while not stop_event.wait(MAINTENANCE_INTERVAL):
        try:
            cache.audio_cache.prune()
            history.prune()
            if auth.limiter is not None:
                auth.limiter.prune()
            audio.player.cleanup_temp()
        except Exception:
            # A failing sweep must not end the thread; the next pass may work,
            # and speech keeps working regardless.
            log.exception("maintenance pass failed")


def describe_startup():
    log.info("engines: %s (default %s)", ", ".join(engines.ENGINES), config.DEFAULT_ENGINE)
    quiet = quiet_hours.status()
    log.info("quiet hours: %s", ", ".join(quiet["windows"]) or "none")
    log.info("cache: %s, history: %s, dashboard: %s",
             "on" if cache.audio_cache.enabled else "off",
             "on" if history.enabled else "off",
             "on" if config.DASHBOARD_ENABLED else "off")
    if config.AUTH_REQUIRED:
        log.info("auth: on, tokens: %s", ", ".join(sorted(config.SPEAK_TOKENS)))
        log.info("auth exempt: %s", ", ".join(config.AUTH_EXEMPT_CIDRS) or "nothing")
        if config.AUTH_EXEMPT_GATEWAY:
            # Worth its own line: it is the difference between speak.sh working
            # and returning 401, and it exempts more than the name suggests.
            log.info("auth: %s is this container's gateway, so callers on the host "
                     "(and anything else reaching the published port through it) "
                     "skip auth; set AUTH_EXEMPT_CIDRS to override",
                     config.AUTH_EXEMPT_GATEWAY)
    if config.AUDIO_ROUTES:
        log.info("audio routes: %s", ", ".join(f"{k}->{v}" for k, v in config.AUDIO_ROUTES.items()))
    receivers = webhooks.registry.receivers
    log.info("webhook receivers: %s", ", ".join(sorted(receivers)) or "none")


def main():
    fatal = config.validate()
    if fatal:
        for problem in fatal:
            log.error("configuration error: %s", problem)
        return 2

    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
    except OSError as e:
        # Not fatal: caching, history and the temp-file playback path all degrade
        # on their own, and speaking is more important than any of them.
        log.warning("cannot create %s (%s); cache and history will be disabled",
                    config.DATA_DIR, e)

    describe_startup()
    audio.player.cleanup_temp()

    queue.start()
    mqtt.bridge.start()

    stop_event = threading.Event()
    threading.Thread(target=maintenance_loop, args=(stop_event,),
                     name="maintenance", daemon=True).start()

    server = api.serve()

    def shutdown(signum, _frame):
        # Compose sends SIGTERM on `down` and on restart. Stopping the player
        # thread first means the current clip is cut off rather than the process
        # lingering for the length of a paragraph someone is having read out.
        log.info("received signal %s; shutting down", signum)
        stop_event.set()
        queue.shutdown()
        mqtt.bridge.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
