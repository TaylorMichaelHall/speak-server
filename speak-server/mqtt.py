"""Optional MQTT front end.

Home automation is where this earns its keep: Home Assistant, Node-RED, an ESP32
button and most hub software can publish an MQTT message far more easily than
they can make an authenticated HTTP POST. Publishing `speak/say` with a payload
of "the back door is open" is a one-line automation.

Everything here is opt-in and non-fatal. No `MQTT_HOST` means the module does
nothing; `paho-mqtt` not installed means a warning and nothing else. The HTTP
server is the primary interface and must never fail to start because a broker is
unreachable.

Topics
    speak/say                 text or the same JSON body as POST /speak
    speak/say/<priority>      priority from the topic, for publishers that can
                              only send a bare string
    speak/status              retained snapshot: queue depth, mute, quiet hours
    speak/event               one message per utterance state change
    speak/cmd/<action>        stop | skip | clear | mute | unmute
"""

import json
import logging
import threading

import audio
import config
from queues import ValidationError, build_utterance, queue

log = logging.getLogger("mqtt")

try:
    import paho.mqtt.client as paho
except ImportError:
    paho = None

# How often the retained status snapshot is refreshed. Frequent enough for a
# dashboard card to feel live, rare enough not to spam a broker's history.
STATUS_INTERVAL = 10


class MqttBridge:
    def __init__(self):
        self.enabled = bool(config.MQTT_HOST)
        self.client = None
        self.connected = False
        self.last_error = None
        self._stop = threading.Event()

    def available(self):
        if not self.enabled:
            return False, "MQTT_HOST is not set"
        if paho is None:
            return False, "paho-mqtt is not installed in this image"
        return True, None

    # ---- lifecycle ----------------------------------------------------

    def start(self):
        ok, reason = self.available()
        if not ok:
            if self.enabled:
                log.warning("MQTT requested but unavailable: %s", reason)
                self.last_error = reason
            return None

        # CallbackAPIVersion is required by paho 2.x and absent in 1.x; support
        # both, because distro packages are still shipping 1.6.
        try:
            self.client = paho.Client(
                callback_api_version=paho.CallbackAPIVersion.VERSION1,
                client_id=config.MQTT_CLIENT_ID,
            )
        except (AttributeError, TypeError):
            self.client = paho.Client(client_id=config.MQTT_CLIENT_ID)

        if config.MQTT_USERNAME:
            self.client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD or None)
        if config.MQTT_TLS:
            self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        # Last will, so subscribers can tell "the server is quiet" from "the
        # server is gone" — the difference matters when you rely on it for alerts.
        self.client.will_set(
            f"{config.MQTT_STATUS_TOPIC}/online", "false", qos=config.MQTT_QOS, retain=True
        )

        try:
            self.client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=60)
        except (OSError, ValueError) as e:
            # connect_async rarely raises, but a bad hostname does it here.
            log.warning("MQTT connect failed: %s", e)
            self.last_error = str(e)
            return None

        # loop_start owns its own thread and reconnects on its own; the status
        # publisher is ours.
        self.client.loop_start()
        queue.on_event(self._on_queue_event)
        thread = threading.Thread(target=self._status_loop, name="mqtt-status", daemon=True)
        thread.start()
        log.info("MQTT bridge connecting to %s:%d", config.MQTT_HOST, config.MQTT_PORT)
        return thread

    def stop(self):
        self._stop.set()
        if self.client is not None:
            try:
                self.client.publish(
                    f"{config.MQTT_STATUS_TOPIC}/online", "false",
                    qos=config.MQTT_QOS, retain=True,
                )
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

    # ---- callbacks ----------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            self.connected = False
            self.last_error = f"broker refused the connection (code {rc})"
            log.warning("MQTT %s", self.last_error)
            return
        self.connected = True
        self.last_error = None
        topics = [
            (config.MQTT_TOPIC, config.MQTT_QOS),
            (f"{config.MQTT_TOPIC}/+", config.MQTT_QOS),
            (f"{_command_root()}/+", config.MQTT_QOS),
        ]
        client.subscribe(topics)
        client.publish(f"{config.MQTT_STATUS_TOPIC}/online", "true",
                       qos=config.MQTT_QOS, retain=True)
        log.info("MQTT connected; subscribed to %s", ", ".join(t for t, _ in topics))
        self._publish_status()

    def _on_disconnect(self, client, userdata, rc, properties=None, reason=None):
        self.connected = False
        # paho's loop reconnects by itself; log at info because a broker restart
        # is routine and shouldn't look like an incident.
        log.info("MQTT disconnected (code %s); will retry", rc)

    def _on_message(self, client, userdata, message):
        try:
            self._handle(message)
        except ValidationError as e:
            log.warning("MQTT %s rejected: %s", message.topic, e)
            self._publish_event("rejected", {"topic": message.topic, "error": str(e)})
        except Exception:
            log.exception("MQTT message on %s failed", message.topic)

    def _handle(self, message):
        topic = message.topic
        raw = message.payload.decode("utf-8", errors="replace").strip()

        if topic.startswith(_command_root() + "/"):
            return self._handle_command(topic.rsplit("/", 1)[-1], raw)

        # A trailing topic segment names the priority: speak/say/high.
        priority = None
        if topic != config.MQTT_TOPIC and topic.startswith(config.MQTT_TOPIC + "/"):
            priority = topic[len(config.MQTT_TOPIC) + 1:]

        payload = raw
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except ValueError:
                pass
        if isinstance(payload, str):
            payload = {"text": payload}
        if priority and "priority" not in payload:
            payload["priority"] = priority
        # MQTT is fire-and-forget by nature: there is nobody to hold a response
        # open for, so never block the network loop waiting for playback.
        payload["wait"] = False

        item = build_utterance(payload, source="mqtt", client=f"mqtt:{config.MQTT_HOST}")
        submission = queue.submit(item)
        log.info("MQTT %s -> %s (%s)", topic, submission.status, item.id)

    def _handle_command(self, action, raw):
        action = action.lower()
        if action == "stop":
            queue.clear()
            queue.skip()
        elif action == "skip":
            queue.skip()
        elif action == "clear":
            queue.clear()
        elif action in ("mute", "unmute"):
            audio.player.set_muted(action == "mute")
        elif action == "volume":
            # Not a queue action, so it isn't handled here — volume is per
            # utterance or per server, deliberately not a global runtime dial
            # that would silently change what every later request sounds like.
            log.warning("MQTT command 'volume' is not supported; set it per utterance")
            return
        else:
            log.warning("MQTT unknown command %r", action)
            return
        log.info("MQTT command %s", action)
        self._publish_status()

    # ---- publishing ---------------------------------------------------

    def _on_queue_event(self, event, payload):
        self._publish_event(event, payload)
        if event in ("queued", "spoke", "failed", "dropped", "started"):
            self._publish_status()

    def _publish_event(self, event, payload):
        if not (self.client and self.connected):
            return
        body = dict(payload)
        body["event"] = event
        try:
            self.client.publish(config.MQTT_EVENT_TOPIC, json.dumps(body, default=str),
                                qos=config.MQTT_QOS)
        except Exception as e:
            log.debug("MQTT event publish failed: %s", e)

    def _publish_status(self):
        if not (self.client and self.connected):
            return
        from quiethours import quiet_hours

        snapshot = queue.snapshot()
        body = {
            "playing": bool(snapshot["playing"]),
            "text": (snapshot["playing"] or {}).get("text"),
            "depth": snapshot["depth"],
            "muted": snapshot["muted"],
            "quiet_hours": quiet_hours.status()["active"],
            "counts": snapshot["counts"],
        }
        try:
            # Retained: a subscriber that connects later still learns the current
            # state without waiting for the next change.
            self.client.publish(config.MQTT_STATUS_TOPIC, json.dumps(body, default=str),
                                qos=config.MQTT_QOS, retain=True)
        except Exception as e:
            log.debug("MQTT status publish failed: %s", e)

    def _status_loop(self):
        while not self._stop.wait(STATUS_INTERVAL):
            self._publish_status()

    def status(self):
        ok, reason = self.available()
        return {
            "configured": self.enabled,
            "available": ok,
            "reason": reason,
            "connected": self.connected,
            "host": config.MQTT_HOST or None,
            "port": config.MQTT_PORT if self.enabled else None,
            "topics": {
                "say": config.MQTT_TOPIC,
                "commands": f"{_command_root()}/<stop|skip|clear|mute|unmute>",
                "status": config.MQTT_STATUS_TOPIC,
                "event": config.MQTT_EVENT_TOPIC,
            } if self.enabled else {},
            "last_error": self.last_error,
        }


def _command_root():
    """Commands live beside the say topic: `speak/say` -> `speak/cmd`. Derived
    rather than configured, so there is one fewer variable to get wrong."""
    base = config.MQTT_TOPIC.rsplit("/", 1)[0] if "/" in config.MQTT_TOPIC else "speak"
    return f"{base}/cmd"


bridge = MqttBridge()
