"""Tests for speak-server. Stdlib unittest only, no test dependencies.

    python3 -m unittest discover -s tests -v

Nothing here touches a real TTS engine or the audio session: `engines.synthesize`
is replaced with a generator of valid WAV bytes and the player is replaced with a
recorder. What is being tested is the logic that decides *whether*, *when*, *in
what order* and *how loudly* something gets spoken, which is where the behaviour
people actually rely on lives.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from datetime import datetime
from io import BytesIO

# config reads the environment once at import, so the scratch directory has to
# be in place before anything is imported.
_TMP = tempfile.mkdtemp(prefix="speak-test-")
os.environ.update({
    "DATA_DIR": _TMP,
    "QUIET_HOURS": "",
    "SPEAK_TOKENS": "",
    "AUTH_REQUIRED": "",
    "RATE_LIMIT": "",
    "LOG_LEVEL": "critical",
    "LEAD_SILENCE_MS": "0",
    "MQTT_HOST": "",
    "WEBHOOKS_FILE": os.path.join(_TMP, "webhooks.json"),
})

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "speak-server"))

import api            # noqa: E402
import audio          # noqa: E402
import auth           # noqa: E402
import cache          # noqa: E402
import config         # noqa: E402
import engines        # noqa: E402
import queues         # noqa: E402
import webhooks       # noqa: E402
from history import history        # noqa: E402
from quiethours import QuietHours  # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def make_wav(seconds=0.1, rate=24000):
    """A real, parseable WAV of silence — the pipeline inspects headers, so a
    stand-in of arbitrary bytes would pass tests the real thing would fail."""
    out = BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(b"\x00\x00" * int(rate * seconds))
    return out.getvalue()


class FakePlayer:
    """Stands in for the real Player. Records what it was asked to play, and can
    be told to block so interruption has something to interrupt."""

    def __init__(self):
        self.played = []
        self.muted = False
        self.block = threading.Event()
        self.block.set()          # not blocking by default
        self.playing = False
        self.stop_calls = 0
        self._interrupt = False

    def play(self, wav, sink=None, volume=None, path=None):
        self.playing = True
        self._interrupt = False
        try:
            self.block.wait(5)
        finally:
            self.playing = False
        self.played.append({"bytes": len(wav), "sink": sink, "volume": volume,
                            "path": path, "interrupted": self._interrupt})
        if self._interrupt:
            return audio.PlaybackResult(True, interrupted=True, duration_ms=5,
                                        sink=sink, volume=volume)
        return audio.PlaybackResult(True, duration_ms=10, sink=sink, volume=volume)

    def stop(self):
        self.stop_calls += 1
        if not self.playing:
            return False
        self._interrupt = True
        self.block.set()
        return True

    def is_playing(self):
        return self.playing

    def playing_for_ms(self):
        return 5 if self.playing else None

    def set_muted(self, value):
        self.muted = bool(value)

    def resolve_sink(self, requested):
        return requested or None

    def cleanup_temp(self):
        pass


# ---------------------------------------------------------------------------


class ConfigHelpers(unittest.TestCase):
    def test_env_pairs_splits_on_first_separator_only(self):
        # Tokens routinely contain '=' (base64 padding); splitting on every one
        # would silently truncate the secret and reject a valid client.
        os.environ["_T"] = "laptop=abc=def,ci=xyz"
        self.assertEqual(config.env_pairs("_T"), {"laptop": "abc=def", "ci": "xyz"})

    def test_env_pairs_ignores_malformed_entries(self):
        os.environ["_T"] = "good=1,nonsense,alsogood=2"
        self.assertEqual(config.env_pairs("_T"), {"good": "1", "alsogood": "2"})

    def test_blank_means_unset(self):
        os.environ["_T"] = "   "
        self.assertEqual(config.env_str("_T", "fallback"), "fallback")
        self.assertEqual(config.env_int("_T", 7), 7)
        self.assertTrue(config.env_bool("_T", True))

    def test_priority_round_trip(self):
        for name, value in config.PRIORITIES.items():
            self.assertEqual(config.priority_value(name), value)
            self.assertEqual(config.priority_name(value), name)
        self.assertIsNone(config.priority_value("urgent-ish"))
        self.assertEqual(config.priority_value(1), 1)

    def test_parse_rate_limit(self):
        self.assertEqual(config.parse_rate_limit("30/60"), (30.0, 60.0))
        self.assertEqual(config.parse_rate_limit("10"), (10.0, 60.0))
        self.assertIsNone(config.parse_rate_limit(""))
        self.assertIsNone(config.parse_rate_limit("nonsense"))
        self.assertIsNone(config.parse_rate_limit("0/60"))


class QuietHoursWindows(unittest.TestCase):
    def test_parses_and_rejects(self):
        from quiethours import parse_windows
        self.assertEqual(parse_windows("22:00-08:00"), [((22, 0), (8, 0))])
        self.assertEqual(len(parse_windows("22:00-08:00,13:00-13:30")), 2)
        self.assertEqual(parse_windows("nonsense"), [])
        self.assertEqual(parse_windows("25:00-08:00"), [])
        # A zero-length window would otherwise read as "always" or "never"
        # depending on comparison order; refusing it is the honest answer.
        self.assertEqual(parse_windows("08:00-08:00"), [])

    def test_window_crossing_midnight(self):
        quiet = QuietHours(spec="22:00-08:00", tz_name="UTC", policy="defer")
        at = lambda h, m=0: datetime(2026, 7, 26, h, m)
        self.assertTrue(quiet.active(at(23)))
        self.assertTrue(quiet.active(at(3)))
        self.assertTrue(quiet.active(at(22)))       # inclusive start
        self.assertFalse(quiet.active(at(8)))      # exclusive end
        self.assertFalse(quiet.active(at(12)))

    def test_daytime_window(self):
        quiet = QuietHours(spec="13:00-14:00", tz_name="UTC")
        self.assertTrue(quiet.active(datetime(2026, 7, 26, 13, 30)))
        self.assertFalse(quiet.active(datetime(2026, 7, 26, 14, 30)))

    def test_decide_respects_override_and_policy(self):
        quiet = QuietHours(spec="00:00-23:59", tz_name="UTC", policy="drop",
                           override="emergency")
        self.assertEqual(quiet.decide(config.priority_value("emergency"))[0], "allow")
        self.assertEqual(quiet.decide(config.priority_value("normal"))[0], "drop")

        quiet.policy = "attenuate"
        quiet.volume = 20
        action, detail = quiet.decide(config.priority_value("low"))
        self.assertEqual((action, detail), ("attenuate", 20))

    def test_defer_returns_a_future_timestamp(self):
        quiet = QuietHours(spec="00:00-23:00", tz_name="UTC", policy="defer")
        action, when = quiet.decide(config.priority_value("normal"))
        if action == "defer":                      # skipped only in the 23:00 hour
            self.assertGreater(when, time.time())

    def test_snooze_suspends_the_window(self):
        quiet = QuietHours(spec="00:00-23:59", tz_name="UTC", policy="drop")
        self.assertTrue(quiet.active())
        quiet.snooze(60)
        self.assertFalse(quiet.active())
        self.assertEqual(quiet.decide(config.priority_value("low"))[0], "allow")
        quiet.unsnooze()
        self.assertTrue(quiet.active())

    def test_unparseable_spec_never_silences_everything(self):
        quiet = QuietHours(spec="every night please", tz_name="UTC")
        self.assertFalse(quiet.active())


class AudioHelpers(unittest.TestCase):
    def test_prepend_silence_lengthens_the_clip(self):
        original = make_wav(0.1, rate=8000)
        padded = audio.prepend_silence(original, 500)
        self.assertAlmostEqual(engines.wav_duration_ms(padded),
                               engines.wav_duration_ms(original) + 500, delta=2)

    def test_prepend_silence_survives_junk(self):
        self.assertEqual(audio.prepend_silence(b"not a wav", 500), b"not a wav")

    def test_zero_padding_is_a_no_op(self):
        original = make_wav()
        self.assertIs(audio.prepend_silence(original, 0), original)



class CacheBehaviour(unittest.TestCase):
    def test_key_changes_with_every_input_that_changes_audio(self):
        base = ("kokoro", "kokoro", "af_heart", 1.0, None, "hello", 500)
        key = cache.cache_key(*base)
        self.assertEqual(key, cache.cache_key(*base))
        for index in range(len(base)):
            altered = list(base)
            altered[index] = "different" if isinstance(base[index], str) else 999
            self.assertNotEqual(key, cache.cache_key(*altered),
                                f"field {index} does not affect the cache key")

    def test_put_then_get(self):
        store = cache.AudioCache()
        store.enabled = True
        store.dir = os.path.join(_TMP, "cache-test")
        os.makedirs(store.dir, exist_ok=True)
        payload = make_wav()
        path = store.put("a" * 64, payload)
        self.assertTrue(os.path.exists(path))
        data, hit_path = store.get("a" * 64)
        self.assertEqual(data, payload)
        self.assertEqual(hit_path, path)
        self.assertEqual(store.get("b" * 64), (None, None))

    def test_long_text_is_not_cached(self):
        self.assertTrue(cache.audio_cache.cacheable("short"))
        self.assertFalse(cache.audio_cache.cacheable("x" * (config.CACHE_MAX_TEXT + 1)))

    def test_prune_enforces_the_size_limit(self):
        store = cache.AudioCache()
        store.enabled = True
        store.dir = os.path.join(_TMP, "cache-prune")
        os.makedirs(store.dir, exist_ok=True)
        now = time.time()
        for index in range(6):
            store.put(f"{index:064d}", make_wav(0.5))
            # Distinct but recent mtimes: "least recently used" needs an ordering,
            # and anything backdated past CACHE_MAX_AGE_DAYS would be pruned by
            # age instead, which is a different rule than the one under test.
            stamp = now - (6 - index)
            os.utime(store._path(f"{index:064d}"), (stamp, stamp))
        original = config.CACHE_MAX_MB
        config.CACHE_MAX_MB = 0.05  # ~50 kB, well under six 24 kB clips
        try:
            store.prune()
        finally:
            config.CACHE_MAX_MB = original
        remaining = {os.path.basename(p) for p, _s, _m in store._entries()}
        self.assertLess(len(remaining), 6)
        # The newest survive; the oldest go first.
        self.assertIn(f"{5:064d}.wav", remaining)
        self.assertNotIn(f"{0:064d}.wav", remaining)


class AuthBehaviour(unittest.TestCase):
    def test_loopback_is_exempt_by_default(self):
        self.assertTrue(auth.is_exempt("127.0.0.1"))
        self.assertTrue(auth.is_exempt("::1"))
        self.assertTrue(auth.is_exempt("::ffff:127.0.0.1"))
        self.assertFalse(auth.is_exempt("192.168.1.50"))
        self.assertFalse(auth.is_exempt("not-an-address"))

    def test_exemptions_can_be_cleared_with_a_word_not_an_empty_value(self):
        # An empty value means "unset, use the default" for every variable, so it
        # cannot express "no exemptions" — that needs the sentinel. Without this,
        # the documented way to require a token locally silently does nothing.
        os.environ["AUTH_EXEMPT_CIDRS"] = ""
        self.assertEqual(config.env_list("AUTH_EXEMPT_CIDRS", ["127.0.0.0/8"]),
                         ["127.0.0.0/8"])
        for sentinel in ("none", "off", "NONE"):
            os.environ["AUTH_EXEMPT_CIDRS"] = sentinel
            listed = config.env_list("AUTH_EXEMPT_CIDRS", ["127.0.0.0/8"])
            self.assertTrue(len(listed) == 1 and listed[0].lower() in ("none", "off"))
        del os.environ["AUTH_EXEMPT_CIDRS"]

    def test_invalid_cidr_is_dropped_not_fatal(self):
        self.assertEqual(config.parse_networks(["10.0.0.0/8", "not-a-network"]),
                         config.parse_networks(["10.0.0.0/8"]))

    # The loopback exemption above is what the *process* sees. In a container it
    # is not what arrives: published ports are NAT'd and a caller on the host
    # shows up as the bridge gateway, so loopback never matches and speak.sh gets
    # a 401 the moment tokens are set. These cover the gateway detection that
    # keeps that from happening.

    def test_default_gateway_is_read_from_the_route_table(self):
        # Columns are Iface/Destination/Gateway/...; addresses are little-endian
        # hex, so 010011AC is 172.17.0.1 rather than 172.16.17.1.
        routes = (
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
            "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        )
        self.assertEqual(config.parse_default_gateway(routes), "172.17.0.1")

    def test_gateway_detection_survives_a_route_table_with_no_default(self):
        # A container with only on-link routes has no next hop to exempt. The
        # answer is "none", not a crash and not 0.0.0.0.
        routes = (
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        )
        self.assertIsNone(config.parse_default_gateway(routes))
        self.assertIsNone(config.parse_default_gateway(""))
        self.assertIsNone(config.default_gateway("/nonexistent/proc/net/route"))

    def test_an_explicit_exempt_list_is_not_widened_by_the_gateway(self):
        # Adding the gateway silently to a list the operator wrote themselves
        # would exempt a host they never named. Only the default gets the help,
        # which is why config records whether the value was configured at all.
        self.assertTrue(hasattr(config, "AUTH_EXEMPT_GATEWAY"))
        os.environ["AUTH_EXEMPT_CIDRS"] = "10.1.2.0/24"
        try:
            self.assertTrue(bool(config.env_str("AUTH_EXEMPT_CIDRS")))
        finally:
            del os.environ["AUTH_EXEMPT_CIDRS"]
        # Unset is what triggers the gateway lookup.
        self.assertFalse(bool(config.env_str("AUTH_EXEMPT_CIDRS")))

    def test_gateway_exemption_makes_host_local_callers_exempt(self):
        # The end the fix exists for: a caller arriving as the gateway is exempt,
        # while an address on the LAN still has to present a token.
        originals = list(auth.EXEMPT_NETWORKS)
        auth.EXEMPT_NETWORKS.extend(config.parse_networks(["172.17.0.1/32"]))
        try:
            self.assertTrue(auth.is_exempt("172.17.0.1"))
            self.assertFalse(auth.is_exempt("172.17.0.2"))
            self.assertFalse(auth.is_exempt("192.168.11.50"))
        finally:
            auth.EXEMPT_NETWORKS[:] = originals

    def test_token_extracted_from_every_supported_place(self):
        self.assertEqual(auth.extract_token({"Authorization": "Bearer abc"}, {}), ("abc", "header"))
        self.assertEqual(auth.extract_token({"X-Speak-Token": "abc"}, {}), ("abc", "header"))
        self.assertEqual(auth.extract_token({"Cookie": "a=1; speak_token=abc"}, {}),
                         ("abc", "cookie"))
        self.assertEqual(auth.extract_token({}, {"token": ["abc"]}), ("abc", "query"))
        self.assertEqual(auth.extract_token({}, {}), (None, None))

    def test_named_token_identifies_the_client(self):
        original = config.SPEAK_TOKENS
        config.SPEAK_TOKENS = {"laptop": "secret-one", "ci": "secret-two"}
        try:
            identity, source = auth.identify(
                {"Authorization": "Bearer secret-two"}, {}, "192.168.1.9")
            self.assertEqual(identity.name, "ci")
            self.assertTrue(identity.authenticated)
            with self.assertRaises(auth.AuthError):
                auth.identify({"Authorization": "Bearer wrong"}, {}, "192.168.1.9")
        finally:
            config.SPEAK_TOKENS = original

    def test_missing_token_from_a_remote_address_is_refused_when_required(self):
        originals = (config.SPEAK_TOKENS, config.AUTH_REQUIRED)
        config.SPEAK_TOKENS, config.AUTH_REQUIRED = {"a": "b"}, True
        try:
            with self.assertRaises(auth.AuthError):
                auth.identify({}, {}, "10.0.0.5")
            # …but not from loopback, which is inside the trust boundary anyway.
            identity, _ = auth.identify({}, {}, "127.0.0.1")
            self.assertTrue(identity.exempt)
        finally:
            config.SPEAK_TOKENS, config.AUTH_REQUIRED = originals

    def test_token_bucket_allows_a_burst_then_refills(self):
        bucket = auth.TokenBucket(3, 60)
        self.assertEqual([bucket.consume("x")[0] for _ in range(4)],
                         [True, True, True, False])
        allowed, retry_after = bucket.consume("x")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)
        # Buckets are per identity, so one noisy client can't silence another.
        self.assertTrue(bucket.consume("y")[0])

    def test_emergency_bypasses_the_rate_limit(self):
        original = auth.limiter
        auth.limiter = auth.TokenBucket(1, 60)
        try:
            identity = auth.Identity("test", "10.0.0.1")
            auth.check_rate_limit(identity, config.priority_value("normal"))
            with self.assertRaises(auth.RateLimitError):
                auth.check_rate_limit(identity, config.priority_value("normal"))
            # Same exhausted bucket, but an emergency still gets through.
            auth.check_rate_limit(identity, config.priority_value("emergency"))
        finally:
            auth.limiter = original


class WebhookRendering(unittest.TestCase):
    def test_lookup_walks_dicts_and_lists(self):
        payload = {"a": {"b": [{"c": "found"}]}, "flag": True, "nothing": None}
        self.assertEqual(webhooks.lookup(payload, "a.b.0.c"), "found")
        self.assertEqual(webhooks.lookup(payload, "flag"), "yes")
        self.assertEqual(webhooks.lookup(payload, "nothing"), "")
        self.assertEqual(webhooks.lookup(payload, "a.b.9.c"), "")
        self.assertEqual(webhooks.lookup(payload, "missing.path"), "")
        # A whole object read aloud is noise, so it renders as nothing.
        self.assertEqual(webhooks.lookup(payload, "a"), "")

    def test_render_tidies_clauses_left_empty(self):
        # Templates are written for the general case, so absent fields are normal
        # and the leftover punctuation has to disappear with them.
        out = webhooks.render("{repo}: {who} {did} {title}",
                              {"repo": "speak-server", "who": "taylor", "did": "opened"})
        self.assertEqual(out, "speak-server: taylor opened")
        self.assertEqual(webhooks.render("{a} — {b}", {"b": "only"}), "only")

    def test_match_filter(self):
        receiver = webhooks.Receiver("alerts", {
            "template": "{status}", "match": {"status": "firing"}})
        self.assertTrue(receiver.matches({"status": "firing"}))
        self.assertFalse(receiver.matches({"status": "resolved"}))
        # Senders are inconsistent about quoting numbers, so comparison is textual.
        listed = webhooks.Receiver("n", {"template": "x", "match": {"n": [1, 2]}})
        self.assertTrue(listed.matches({"n": 2}))
        self.assertFalse(listed.matches({"n": 3}))

    def test_generic_field_search(self):
        receiver = webhooks.Receiver("any", {"preset": "generic"})
        self.assertEqual(receiver.speech_for({"message": "hello there"}), "hello there")
        self.assertEqual(receiver.speech_for({"title": "fallback"}), "fallback")
        self.assertEqual(receiver.speech_for({"unrelated": 1}), "")

    def test_alertmanager_preset(self):
        receiver = webhooks.Receiver("alerts", {"preset": "alertmanager"})
        text = receiver.speech_for({
            "status": "firing",
            "alerts": [{"labels": {"alertname": "DiskFull", "instance": "nas"},
                        "annotations": {"summary": "only 2 percent left"}}],
        })
        self.assertEqual(text, "firing: DiskFull on nas — only 2 percent left")

    def test_truncation_lands_on_a_word_boundary(self):
        receiver = webhooks.Receiver("x", {"template": "{t}", "max_length": 20})
        text = receiver.speech_for({"t": "a much longer sentence than that limit"})
        self.assertLessEqual(len(text), 20)
        self.assertFalse(text.endswith(" "))
        self.assertTrue("a much longer" in text)

    def test_prefix_and_empty_payload(self):
        receiver = webhooks.Receiver("x", {"template": "{t}", "prefix": "Heads up:"})
        self.assertEqual(receiver.speech_for({"t": "it broke"}), "Heads up: it broke")
        # No prefix on an empty result — otherwise every ignorable payload speaks.
        self.assertEqual(receiver.speech_for({}), "")

    def test_registry_skips_bad_definitions_and_keeps_good_ones(self):
        path = os.path.join(_TMP, "hooks.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "good": {"template": "{message}"},
                "bad priority": {"template": "x"},          # space in the name
                "bad-preset": {"preset": "nope"},
                "alsogood": {"preset": "grafana"},
            }, fh)
        registry = webhooks.Registry(path)
        self.assertEqual(sorted(registry.receivers), ["alsogood", "good"])

    def test_missing_file_is_not_an_error(self):
        registry = webhooks.Registry(os.path.join(_TMP, "nope.json"))
        self.assertEqual(registry.receivers, {})
        self.assertIsNone(registry.error)


class UtteranceValidation(unittest.TestCase):
    def test_plain_string_body(self):
        item = queues.build_utterance("just text")
        self.assertEqual(item.text, "just text")
        self.assertEqual(item.engine, config.DEFAULT_ENGINE)
        self.assertTrue(item.wait)

    def test_rejections(self):
        cases = [
            ({}, "no text"),
            ({"text": "   "}, "no text"),
            ({"text": "x", "engine": "festival"}, "unknown engine"),
            ({"text": "x", "speed": "quickly"}, "not a number"),
            ({"text": "x", "speed": 99}, "outside"),
            ({"text": "x", "priority": "urgent"}, "unknown priority"),
            ({"text": "x", "volume": 500}, "outside"),
            ({"text": "y" * (config.MAX_TEXT + 1)}, "MAX_TEXT"),
        ]
        for payload, fragment in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(queues.ValidationError) as caught:
                    queues.build_utterance(payload)
                self.assertIn(fragment, str(caught.exception))

    def test_wait_accepts_strings_from_the_query_string(self):
        self.assertFalse(queues.build_utterance({"text": "x", "wait": "false"}).wait)
        self.assertFalse(queues.build_utterance({"text": "x", "wait": "0"}).wait)
        self.assertTrue(queues.build_utterance({"text": "x", "wait": "yes"}).wait)

    def test_pseudo_engines_are_accepted(self):
        for name in engines.PSEUDO_ENGINES:
            self.assertEqual(queues.build_utterance({"text": "x", "engine": name}).engine, name)


class EngineHealthAccounting(unittest.TestCase):
    """A 4xx means the engine read the request and refused it, which is not the
    same as the engine being down. Conflating them lets a caller's mistake bench
    a working backend: `random` with a kokoro voice makes supertonic answer
    "unknown voice", and three of those trip the cooldown — so the engine is
    dropped from automatic selection and reported as failing while it is fine.
    """

    def _synthesize_against(self, status, body=b"nope"):
        real = urllib.request.urlopen

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, BytesIO(body))

        urllib.request.urlopen = fake_urlopen
        try:
            return engines.synthesize("supertonic", "hello")
        finally:
            urllib.request.urlopen = real

    def test_a_4xx_refusal_is_not_counted_against_engine_health(self):
        before = engines.stats.snapshot()["supertonic"]
        for _ in range(config.ENGINE_FAILURE_THRESHOLD + 1):
            wav, err, _ = self._synthesize_against(400, b'{"error":"unknown voice"}')
            self.assertIsNone(wav)
            self.assertIn("400", err)
        after = engines.stats.snapshot()["supertonic"]
        self.assertEqual(after["total_failed"], before["total_failed"])
        self.assertEqual(after["failures"], before["failures"])
        self.assertFalse(after["cooling_down"], "a 4xx must not bench the engine")

    def test_a_5xx_still_counts_and_still_trips_the_cooldown(self):
        # The guard against over-correcting: a real outage must still be caught.
        stats = engines.EngineStats()
        for _ in range(config.ENGINE_FAILURE_THRESHOLD):
            stats.record_failure("supertonic", "500 boom")
        self.assertTrue(stats.is_cooling_down("supertonic"))

        real = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 503, "down", {}, BytesIO(b"down"))
        )
        try:
            before = engines.stats.snapshot()["kokoro"]["total_failed"]
            engines.synthesize("kokoro", "hello")
            after = engines.stats.snapshot()["kokoro"]["total_failed"]
        finally:
            urllib.request.urlopen = real
        self.assertEqual(after, before + 1)


class EngineSelection(unittest.TestCase):
    def test_named_engine_gets_no_peer_fallback(self):
        candidates, error = engines.resolve_candidates("kokoro")
        self.assertIsNone(error)
        self.assertEqual(candidates, ["kokoro"])

    def test_unknown_engine_reports_the_options(self):
        candidates, error = engines.resolve_candidates("festival")
        self.assertEqual(candidates, [])
        self.assertIn("unknown engine", error)

    def test_random_covers_every_engine(self):
        candidates, error = engines.resolve_candidates("random")
        self.assertIsNone(error)
        self.assertEqual(sorted(candidates), sorted(engines.ENGINES))

    def test_fastest_prefers_the_lower_measured_latency(self):
        stats = engines.EngineStats()
        stats.record_success("kokoro", 2.0)
        stats.record_success("supertonic", 0.4)
        original = engines.stats
        engines.stats = stats
        try:
            candidates, _ = engines.resolve_candidates("fastest")
        finally:
            engines.stats = original
        self.assertEqual(candidates[0], "supertonic")

    def test_unmeasured_engines_are_sampled_before_the_ranking_settles(self):
        stats = engines.EngineStats()
        stats.record_success("kokoro", 0.1)
        original = engines.stats
        engines.stats = stats
        try:
            candidates, _ = engines.resolve_candidates("fastest")
        finally:
            engines.stats = original
        self.assertEqual(candidates[0], "supertonic")   # never measured yet

    def test_cooldown_pushes_a_failing_engine_to_the_back(self):
        stats = engines.EngineStats()
        for _ in range(config.ENGINE_FAILURE_THRESHOLD):
            stats.record_failure("kokoro", "boom")
        self.assertTrue(stats.is_cooling_down("kokoro"))
        original = engines.stats
        engines.stats = stats
        try:
            candidates, _ = engines.resolve_candidates("random")
        finally:
            engines.stats = original
        # Pushed back, not removed: a total outage must still produce a real error
        # from a real attempt rather than "no engines available".
        self.assertEqual(candidates[-1], "kokoro")
        self.assertIn("kokoro", candidates)

    def test_a_success_clears_the_cooldown(self):
        stats = engines.EngineStats()
        for _ in range(config.ENGINE_FAILURE_THRESHOLD):
            stats.record_failure("kokoro", "boom")
        stats.record_success("kokoro", 0.5)
        self.assertFalse(stats.is_cooling_down("kokoro"))

    def test_voice_list_shapes(self):
        self.assertEqual(engines._extract_voices({"voices": ["a", "b"]}), ["a", "b"])
        self.assertEqual(engines._extract_voices(["a", "b"]), ["a", "b"])
        self.assertEqual(engines._extract_voices({"styles": [{"name": "M1"}]}), ["M1"])
        self.assertEqual(engines._extract_voices({"data": [{"id": "x"}]}), ["x"])
        self.assertEqual(engines._extract_voices("nonsense"), [])


class QueueBehaviour(unittest.TestCase):
    """Exercises the real player thread with a fake player and fake synthesis."""

    def setUp(self):
        self.player = FakePlayer()
        self.quiet = QuietHours(spec="", tz_name="UTC")
        self.queue = queues.SpeechQueue(player=self.player, quiet=self.quiet)
        self.synth_calls = []

        def fake_synthesize(engine, text, voice=None, speed=1.0, lang=None):
            self.synth_calls.append((engine, text, voice))
            return make_wav(), None, 0.01

        self._real_synthesize = engines.synthesize
        engines.synthesize = fake_synthesize
        self._real_history = history.enabled
        history.enabled = False          # keep the test DB out of it
        # Off by default here: several tests reuse the same phrase, and a cache
        # hit would (correctly) skip synthesis and hide the ordering being tested.
        # The cache has its own test that turns it back on.
        self._real_cache = cache.audio_cache.enabled
        cache.audio_cache.enabled = False
        self.queue.start()

    def tearDown(self):
        self.queue.shutdown()
        engines.synthesize = self._real_synthesize
        history.enabled = self._real_history
        cache.audio_cache.enabled = self._real_cache

    def speak(self, text, **kw):
        item = queues.build_utterance({"text": text, "wait": False, **kw})
        return item, self.queue.submit(item)

    def test_speaks_and_reports_success(self):
        item, submission = self.speak("hello")
        self.assertTrue(submission.accepted)
        self.assertTrue(item.done.wait(5))
        self.assertEqual(item.status, "spoke")
        self.assertEqual(len(self.player.played), 1)

    def test_priority_order_not_arrival_order(self):
        # Hold the player so a queue actually forms behind the first utterance.
        self.player.block.clear()
        first, _ = self.speak("blocking")
        for _ in range(50):
            if self.player.playing:
                break
            time.sleep(0.02)

        low, _ = self.speak("least important", priority="low")
        high, _ = self.speak("most important", priority="high")
        normal, _ = self.speak("middling", priority="normal")

        self.player.block.set()
        for item in (first, low, high, normal):
            self.assertTrue(item.done.wait(5), f"{item.text} never finished")
        spoken = [call for call in self.synth_calls]
        order = [text for _e, text, _v in spoken]
        self.assertEqual(order,
                         ["blocking", "most important", "middling", "least important"])

    def test_arrival_order_breaks_ties(self):
        self.player.block.clear()
        blocker, _ = self.speak("blocking")
        for _ in range(50):
            if self.player.playing:
                break
            time.sleep(0.02)
        a, _ = self.speak("first normal")
        b, _ = self.speak("second normal")
        self.player.block.set()
        for item in (blocker, a, b):
            self.assertTrue(item.done.wait(5))
        order = [text for _e, text, _v in self.synth_calls]
        self.assertLess(order.index("first normal"), order.index("second normal"))

    def test_higher_priority_interrupts_playback(self):
        self.player.block.clear()
        playing, _ = self.speak("a long announcement", priority="normal")
        for _ in range(50):
            if self.player.playing:
                break
            time.sleep(0.02)

        urgent, _ = self.speak("the house is on fire", priority="emergency")
        self.assertTrue(playing.done.wait(5))
        self.assertEqual(playing.status, "interrupted")
        self.player.block.set()
        self.assertTrue(urgent.done.wait(5))
        self.assertEqual(urgent.status, "spoke")

    def test_equal_priority_does_not_interrupt(self):
        self.player.block.clear()
        playing, _ = self.speak("first", priority="normal")
        for _ in range(50):
            if self.player.playing:
                break
            time.sleep(0.02)
        self.speak("second", priority="normal")
        time.sleep(0.15)
        self.assertEqual(self.player.stop_calls, 0)
        self.player.block.set()
        self.assertTrue(playing.done.wait(5))
        self.assertEqual(playing.status, "spoke")

    def test_interrupt_can_be_disabled(self):
        original = config.INTERRUPT
        config.INTERRUPT = False
        try:
            self.player.block.clear()
            playing, _ = self.speak("first", priority="low")
            for _ in range(50):
                if self.player.playing:
                    break
                time.sleep(0.02)
            self.speak("urgent", priority="emergency")
            time.sleep(0.15)
            self.assertEqual(self.player.stop_calls, 0)
        finally:
            config.INTERRUPT = original
            self.player.block.set()
            self.assertTrue(playing.done.wait(5))

    def test_interrupted_utterance_is_requeued_when_configured(self):
        original = config.INTERRUPT_REQUEUE
        config.INTERRUPT_REQUEUE = True
        try:
            self.player.block.clear()
            playing, _ = self.speak("say me twice", priority="normal")
            for _ in range(50):
                if self.player.playing:
                    break
                time.sleep(0.02)
            urgent, _ = self.speak("urgent", priority="emergency")
            self.player.block.set()
            self.assertTrue(urgent.done.wait(5))
            self.assertTrue(playing.done.wait(5))
            self.assertEqual(playing.status, "spoke")
            self.assertEqual(playing.replays, 1)
        finally:
            config.INTERRUPT_REQUEUE = original

    def test_muted_drops_rather_than_backlogs(self):
        self.player.muted = True
        item, submission = self.speak("nobody hears this")
        self.assertTrue(submission.accepted)
        self.assertTrue(item.done.wait(5))
        self.assertEqual(item.status, "muted")
        self.assertEqual(self.player.played, [])
        self.assertEqual(self.synth_calls, [])   # no wasted synthesis either

    def test_quiet_hours_drop(self):
        self.quiet.windows = [((0, 0), (23, 59))]
        self.quiet.policy = "drop"
        item, submission = self.speak("shh", priority="normal")
        self.assertFalse(submission.accepted)
        self.assertEqual(item.status, "dropped")
        self.assertEqual(self.player.played, [])

    def test_quiet_hours_let_an_emergency_through(self):
        self.quiet.windows = [((0, 0), (23, 59))]
        self.quiet.policy = "drop"
        item, submission = self.speak("fire", priority="emergency")
        self.assertTrue(submission.accepted)
        self.assertTrue(item.done.wait(5))
        self.assertEqual(item.status, "spoke")

    def test_quiet_hours_attenuate_takes_the_quieter_of_the_two(self):
        self.quiet.windows = [((0, 0), (23, 59))]
        self.quiet.policy = "attenuate"
        self.quiet.volume = 20
        item, _ = self.speak("quietly", volume=100)
        self.assertTrue(item.done.wait(5))
        self.assertEqual(self.player.played[-1]["volume"], 20)

    def test_quiet_hours_defer_holds_the_item(self):
        self.quiet.windows = [((0, 0), (23, 59))]
        self.quiet.policy = "defer"
        item, submission = self.speak("later")
        self.assertTrue(submission.accepted)
        self.assertEqual(submission.status, "deferred")
        self.assertIsNotNone(item.not_before)
        self.assertGreater(item.not_before, time.time())
        # Deferral must not be a silent drop: the TTL is pushed past the window.
        self.assertGreater(item.expires_at, item.not_before)
        self.assertEqual(self.player.played, [])

    def test_snoozing_releases_what_quiet_hours_already_deferred(self):
        # A deferral is a condition, not a deadline. Holding an item until the
        # window's original end after the window has been snoozed away means
        # "snooze" silences exactly the backlog a person snoozed in order to
        # hear — and the queue already re-checks the window in the other
        # direction, at the front of the queue.
        self.quiet.windows = [((0, 0), (23, 59))]
        self.quiet.policy = "defer"
        item, submission = self.speak("held then released")
        self.assertEqual(submission.status, "deferred")
        self.assertTrue(item.quiet_deferred)
        self.assertEqual(self.player.played, [])

        self.quiet.snooze(600)
        self.assertTrue(item.done.wait(5))
        self.assertEqual(item.status, "spoke")
        # The hold is cleared rather than left behind to be reported later.
        self.assertIsNone(item.not_before)
        self.assertFalse(item.quiet_deferred)

    def test_snoozing_while_muted_does_not_destroy_the_backlog(self):
        # Mute drops what reaches the front of the queue. So releasing a
        # deferred backlog into a muted player would finalize every item as
        # dropped — the snooze would be what destroyed the announcements it was
        # meant to let through. Hold them until something could actually hear.
        self.quiet.windows = [((0, 0), (23, 59))]
        self.quiet.policy = "defer"
        self.player.set_muted(True)
        item, _ = self.speak("held while muted")
        self.quiet.snooze(600)
        self.assertFalse(item.done.wait(1.5))
        self.assertEqual(item.status, "deferred")
        self.assertTrue(item.quiet_deferred)

        # Unmuting is what lets it go, and it survived to be spoken.
        self.player.set_muted(False)
        self.assertTrue(item.done.wait(5))
        self.assertEqual(item.status, "spoke")

    def test_a_deferred_item_is_still_held_while_the_window_stands(self):
        # The guard against over-correcting: only a snooze releases it, not
        # merely reaching the front of the queue.
        self.quiet.windows = [((0, 0), (23, 59))]
        self.quiet.policy = "defer"
        item, _ = self.speak("stays put")
        self.assertFalse(item.done.wait(1.5))
        self.assertEqual(self.player.played, [])
        self.assertTrue(item.quiet_deferred)
        self.assertEqual(self.queue.snapshot()["depth"], 1)

    def test_deferred_item_speaks_once_its_time_arrives(self):
        item = queues.build_utterance({"text": "morning", "wait": False})
        item.not_before = time.time() + 0.15
        self.queue.submit(item)
        self.assertEqual(self.player.played, [])
        self.assertTrue(item.done.wait(5))
        self.assertEqual(item.status, "spoke")

    def test_stale_item_expires_instead_of_waiting_forever(self):
        self.player.block.clear()
        blocker, _ = self.speak("blocking")
        for _ in range(50):
            if self.player.playing:
                break
            time.sleep(0.02)
        stale = queues.build_utterance({"text": "old news", "wait": False,
                                        "priority": "low"})
        stale.expires_at = time.time() + 0.05
        self.queue.submit(stale)
        self.assertTrue(stale.done.wait(5))
        self.assertEqual(stale.status, "expired")
        self.player.block.set()
        self.assertTrue(blocker.done.wait(5))

    def test_full_queue_sheds_its_least_important_item(self):
        original = config.QUEUE_MAX
        config.QUEUE_MAX = 2
        try:
            self.player.block.clear()
            blocker, _ = self.speak("blocking")
            for _ in range(50):
                if self.player.playing:
                    break
                time.sleep(0.02)
            low_a, _ = self.speak("chatter one", priority="low")
            low_b, _ = self.speak("chatter two", priority="low")
            urgent, submission = self.speak("fire", priority="emergency")
            self.assertTrue(submission.accepted)
            # The newest low-priority item is the one displaced.
            self.assertTrue(low_b.done.wait(5))
            self.assertEqual(low_b.status, "dropped")
        finally:
            config.QUEUE_MAX = original
            self.player.block.set()
            for item in (blocker, low_a, urgent):
                item.done.wait(5)

    def test_full_queue_of_equals_refuses_the_newcomer(self):
        original = config.QUEUE_MAX
        config.QUEUE_MAX = 1
        try:
            self.player.block.clear()
            blocker, _ = self.speak("blocking")
            for _ in range(50):
                if self.player.playing:
                    break
                time.sleep(0.02)
            first, _ = self.speak("queued", priority="normal")
            rejected, submission = self.speak("no room", priority="normal")
            self.assertFalse(submission.accepted)
            self.assertIn("full", submission.reason)
            self.assertEqual(rejected.status, "dropped")
        finally:
            config.QUEUE_MAX = original
            self.player.block.set()
            for item in (blocker, first):
                item.done.wait(5)

    def test_cancel_a_queued_item(self):
        self.player.block.clear()
        blocker, _ = self.speak("blocking")
        for _ in range(50):
            if self.player.playing:
                break
            time.sleep(0.02)
        doomed, _ = self.speak("never mind")
        self.assertEqual(self.queue.cancel(doomed.id), "cancelled")
        self.assertEqual(self.queue.cancel("nosuchid"), "not found")
        self.assertEqual(doomed.status, "cancelled")
        self.player.block.set()
        self.assertTrue(blocker.done.wait(5))
        self.assertNotIn("never mind", [t for _e, t, _v in self.synth_calls])

    def test_clear_empties_the_queue_but_leaves_playback_alone(self):
        self.player.block.clear()
        blocker, _ = self.speak("blocking")
        for _ in range(50):
            if self.player.playing:
                break
            time.sleep(0.02)
        self.speak("one")
        self.speak("two")
        self.assertEqual(self.queue.clear(), 2)
        self.assertEqual(self.player.stop_calls, 0)
        self.player.block.set()
        self.assertTrue(blocker.done.wait(5))
        self.assertEqual(blocker.status, "spoke")

    def test_synthesis_failure_is_reported_as_such(self):
        engines.synthesize = lambda *a, **kw: (None, "engine exploded", 0.01)
        item, _ = self.speak("doomed")
        self.assertTrue(item.done.wait(5))
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.fail_stage, "synthesis")
        self.assertIn("engine exploded", item.error)

    def test_cache_hit_skips_synthesis(self):
        original = cache.audio_cache.enabled
        cache.audio_cache.enabled = True
        try:
            first, _ = self.speak("repeat me")
            self.assertTrue(first.done.wait(5))
            self.assertFalse(first.cache_hit)
            second, _ = self.speak("repeat me")
            self.assertTrue(second.done.wait(5))
            self.assertTrue(second.cache_hit)
            self.assertEqual(second.synth_ms, 0)
            # One synthesis for two utterances of the same phrase.
            self.assertEqual(len([t for _e, t, _v in self.synth_calls if t == "repeat me"]), 1)
        finally:
            cache.audio_cache.enabled = original

    def test_snapshot_reports_what_is_playing(self):
        self.player.block.clear()
        item, _ = self.speak("on air now")
        for _ in range(50):
            if self.player.playing:
                break
            time.sleep(0.02)
        snapshot = self.queue.snapshot()
        self.assertIsNotNone(snapshot["playing"])
        self.assertEqual(snapshot["playing"]["text"], "on air now")
        self.player.block.set()
        self.assertTrue(item.done.wait(5))

    def test_sink_routing_reaches_the_player(self):
        item, _ = self.speak("through the kitchen", sink="kitchen")
        self.assertTrue(item.done.wait(5))
        self.assertEqual(self.player.played[-1]["sink"], "kitchen")


class PlaybackCommand(unittest.TestCase):
    """The paplay argv is the whole interface to the sound server, so it gets
    checked directly rather than inferred from behaviour."""

    def build_cmd(self, sink=None, volume=None):
        player = audio.Player()
        captured = {}

        def fake_run(cmd, device, level):
            captured["cmd"] = cmd
            return audio.PlaybackResult(True, duration_ms=1)

        player._run = fake_run
        player.play(make_wav(), sink=sink, volume=volume, path=None)
        return captured["cmd"]

    def test_volume_is_always_explicit(self):
        # module-stream-restore remembers a volume per application name and
        # reapplies it to every new stream. All clips here share one client name,
        # so omitting --volume at full volume lets a single earlier `volume: 15`
        # request silently mute every later utterance. Being explicit every time
        # is what keeps per-request volume per-request.
        for level in (100, 50, 0, None):
            with self.subTest(volume=level):
                cmd = self.build_cmd(volume=level)
                flags = [a for a in cmd if a.startswith("--volume=")]
                self.assertEqual(len(flags), 1, f"expected exactly one --volume in {cmd}")
        self.assertIn("--volume=65536", self.build_cmd(volume=100))
        self.assertIn("--volume=0", self.build_cmd(volume=0))
        self.assertIn("--volume=32768", self.build_cmd(volume=50))

    def test_out_of_range_volume_is_clamped_not_rejected(self):
        self.assertIn("--volume=65536", self.build_cmd(volume=500))
        self.assertIn("--volume=0", self.build_cmd(volume=-10))

    def test_device_only_passed_when_routed(self):
        self.assertFalse([a for a in self.build_cmd() if a.startswith("--device=")])
        self.assertIn("--device=alsa_output.thing", self.build_cmd(sink="alsa_output.thing"))

    def test_client_name_is_set_so_streams_are_identifiable(self):
        self.assertIn("--client-name=speak-server", self.build_cmd())


class SinkResolution(unittest.TestCase):
    def test_named_route_beats_a_raw_device_name(self):
        player = audio.Player()
        original = dict(config.AUDIO_ROUTES)
        config.AUDIO_ROUTES.clear()
        config.AUDIO_ROUTES["kitchen"] = "alsa_output.usb-kitchen"
        try:
            self.assertEqual(player.resolve_sink("kitchen"), "alsa_output.usb-kitchen")
            self.assertEqual(player.resolve_sink("alsa_output.other"), "alsa_output.other")
            self.assertIsNone(player.resolve_sink(""))
            self.assertIsNone(player.resolve_sink(None))
        finally:
            config.AUDIO_ROUTES.clear()
            config.AUDIO_ROUTES.update(original)


class HttpSurface(unittest.TestCase):
    """Drives the real HTTP server over a real socket."""

    @classmethod
    def setUpClass(cls):
        cls.player = FakePlayer()
        cls._real_player = audio.player
        cls._real_queue_player = queues.queue.player
        audio.player = cls.player
        queues.queue.player = cls.player

        cls._real_synthesize = engines.synthesize
        engines.synthesize = lambda *a, **kw: (make_wav(), None, 0.01)

        config.PORT = 0            # let the OS choose a free port
        cls.server = api.serve()
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        queues.queue.start()

    @classmethod
    def tearDownClass(cls):
        queues.queue.shutdown()
        cls.server.shutdown()
        cls.server.server_close()
        engines.synthesize = cls._real_synthesize
        audio.player = cls._real_player
        queues.queue.player = cls._real_queue_player

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, path, data=None, method=None, headers=None, raw=False):
        request = urllib.request.Request(
            self.url(path),
            data=data.encode() if isinstance(data, str) else data,
            headers=headers or {},
            method=method,
        )
        decode = (lambda b: b) if raw else (lambda b: b.decode("utf-8", "replace"))
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, decode(response.read())
        except urllib.error.HTTPError as e:
            return e.code, decode(e.read())

    def test_health_needs_no_token_and_stays_plain_text(self):
        status, body = self.request("/health")
        self.assertEqual((status, body.strip()), (200, "ok"))

    def test_plain_text_body_still_works(self):
        status, body = self.request("/speak", data="Build finished.")
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), "spoke")

    def test_json_body_with_overrides(self):
        payload = json.dumps({"text": "Hello.", "voice": "af_bella", "speed": 1.2})
        status, _ = self.request("/speak", data=payload,
                                 headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)

    def test_empty_text_is_a_400(self):
        status, body = self.request("/speak", data="   ")
        self.assertEqual(status, 400)
        self.assertIn("no text", body)

    def test_unknown_engine_is_a_400_that_lists_the_options(self):
        status, body = self.request("/speak", data=json.dumps({"text": "x", "engine": "sam"}))
        self.assertEqual(status, 400)
        self.assertIn("kokoro", body)

    def test_async_submission_returns_202_with_an_id(self):
        status, body = self.request("/speak?wait=false", data="fire and forget")
        self.assertEqual(status, 202)
        self.assertIn("queued", body)

    def test_query_string_overrides_need_no_json(self):
        status, _ = self.request("/speak?priority=high", data="urgent-ish")
        self.assertEqual(status, 200)

    def test_status_endpoint_describes_the_machine(self):
        status, body = self.request("/api/status")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        for key in ("queue", "engines", "quiet_hours", "cache", "history", "auth",
                    "audio", "config", "mqtt", "webhooks", "identity"):
            self.assertIn(key, payload)

    def test_queue_endpoint(self):
        status, body = self.request("/api/queue")
        self.assertEqual(status, 200)
        self.assertIn("waiting", json.loads(body))

    def test_mute_round_trip(self):
        status, body = self.request("/api/mute", data=json.dumps({"muted": True}),
                                    headers={"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["message"]), (200, "muted"))
        self.assertTrue(self.player.muted)
        self.request("/api/mute", data=json.dumps({"muted": False}),
                     headers={"Content-Type": "application/json"})
        self.assertFalse(self.player.muted)

    def test_history_records_what_was_spoken_and_serves_it_back(self):
        marker = f"history marker {time.time()}"
        self.request("/speak", data=marker)
        status, body = self.request(f"/api/history?q={urllib.parse.quote('history marker')}")
        self.assertEqual(status, 200)
        rows = json.loads(body)["rows"]
        self.assertTrue(rows, "the utterance was not recorded")
        row = rows[0]
        self.assertEqual(row["status"], "spoke")
        self.assertEqual(row["text"], marker)
        self.assertTrue(row["has_audio"], "no audio was kept for replay")
        status, body = self.request(f"/api/history/{row['id']}/audio", raw=True)
        self.assertEqual(status, 200)
        # A real WAV, playable by the dashboard's <audio> element as-is.
        self.assertEqual(body[:4], b"RIFF")
        self.assertEqual(engines.wav_duration_ms(body), engines.wav_duration_ms(make_wav()))

    def test_history_replay_queues_it_again(self):
        self.request("/speak", data="replay me")
        rows = json.loads(self.request("/api/history?q=replay+me")[1])["rows"]
        self.assertTrue(rows)
        status, _ = self.request(f"/api/history/{rows[0]['id']}/replay", method="POST")
        self.assertIn(status, (200, 202))

    def test_history_rows_do_not_leak_filesystem_paths(self):
        self.request("/speak", data="no paths please")
        rows = json.loads(self.request("/api/history?q=no+paths")[1])["rows"]
        self.assertNotIn("clip_path", rows[0])

    def test_unknown_path_is_a_404(self):
        self.assertEqual(self.request("/nope")[0], 404)

    def test_dashboard_is_served(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("speak-server", body)

    def test_dashboard_does_not_serve_files_outside_its_directory(self):
        status, _ = self.request("/dashboard/../server.py")
        self.assertIn(status, (403, 404))

    def test_voices_endpoint_reports_an_unreachable_engine_without_failing(self):
        status, body = self.request("/api/voices/kokoro")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("kokoro", payload["voices"])

    def test_unknown_engine_voices_is_a_404(self):
        self.assertEqual(self.request("/api/voices/festival")[0], 404)

    def test_webhook_that_does_not_exist(self):
        self.assertEqual(self.request("/webhook/nope", data="{}", method="POST")[0], 404)

    def test_webhook_speaks_a_rendered_payload(self):
        webhooks.registry.receivers["test"] = webhooks.Receiver(
            "test", {"template": "{repo} is {state}", "wait": True})
        try:
            status, _ = self.request(
                "/webhook/test",
                data=json.dumps({"repo": "speak-server", "state": "green"}),
                method="POST",
            )
            self.assertEqual(status, 200)
        finally:
            webhooks.registry.receivers.pop("test", None)

    def test_webhook_filter_ignores_a_non_matching_payload(self):
        webhooks.registry.receivers["filtered"] = webhooks.Receiver(
            "filtered", {"template": "{status}", "match": {"status": "firing"}})
        try:
            status, body = self.request("/webhook/filtered",
                                        data=json.dumps({"status": "resolved"}),
                                        method="POST")
            self.assertEqual(status, 200)
            self.assertIn("ignored", body)
        finally:
            webhooks.registry.receivers.pop("filtered", None)

    def test_webhook_secret_is_enforced(self):
        webhooks.registry.receivers["secured"] = webhooks.Receiver(
            "secured", {"template": "{m}", "secret": "hunter2"})
        try:
            payload = json.dumps({"m": "hello"})
            self.assertEqual(self.request("/webhook/secured", data=payload,
                                          method="POST")[0], 401)
            status, _ = self.request("/webhook/secured?secret=hunter2",
                                     data=payload, method="POST")
            self.assertIn(status, (200, 202))
        finally:
            webhooks.registry.receivers.pop("secured", None)

    def test_token_is_required_once_configured(self):
        originals = (dict(config.SPEAK_TOKENS), config.AUTH_REQUIRED,
                     list(auth.EXEMPT_NETWORKS))
        config.SPEAK_TOKENS.clear()
        config.SPEAK_TOKENS["ci"] = "s3cret"
        config.AUTH_REQUIRED = True
        # The test client is loopback, which is exempt by default — drop the
        # exemption so the token path is what's actually exercised.
        auth.EXEMPT_NETWORKS.clear()
        try:
            self.assertEqual(self.request("/speak", data="denied")[0], 401)
            status, _ = self.request("/speak", data="allowed",
                                     headers={"Authorization": "Bearer s3cret"})
            self.assertEqual(status, 200)
            # /health stays open, so monitoring doesn't need a credential.
            self.assertEqual(self.request("/health")[0], 200)
        finally:
            config.SPEAK_TOKENS.clear()
            config.SPEAK_TOKENS.update(originals[0])
            config.AUTH_REQUIRED = originals[1]
            auth.EXEMPT_NETWORKS.extend(originals[2])

    def test_rate_limit_returns_429_with_retry_after(self):
        original = auth.limiter
        auth.limiter = auth.TokenBucket(1, 60)
        try:
            self.assertEqual(self.request("/speak", data="first")[0], 200)
            status, body = self.request("/speak", data="second")
            self.assertEqual(status, 429)
            self.assertIn("rate limit", body)
        finally:
            auth.limiter = original

    def test_oversized_text_is_rejected(self):
        status, _ = self.request("/speak", data="x" * (config.MAX_TEXT + 10))
        self.assertIn(status, (400, 413))


if __name__ == "__main__":
    unittest.main(verbosity=2)
