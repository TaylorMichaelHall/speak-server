# speak-server

Give the agents (or anything else) on your machine a voice. A compose stack built
around one small server and a pluggable set of TTS engines:

- **speak-server** — a Python HTTP server on port **8899**, the only thing
  callers talk to. POST it text; it synthesizes via the chosen engine and plays
  the audio **on the host's speakers** through the mounted PulseAudio/PipeWire
  socket. No audio handling client-side.

Engines behind it, none of them published to the host — speak-server reaches them
over the compose network:

- **kokoro** (default) — [Kokoro FastAPI](https://github.com/remsky/Kokoro-FastAPI)
  (CPU image), an OpenAI-compatible TTS server.
- **supertonic** (opt-in, off by default) —
  [Supertonic](https://github.com/supertone-inc/supertonic) (`supertonic serve`),
  with voices `M1`–`M5` and `F1`–`F5`. Enable it with one line in `.env`.

The point is to give tools on **other** machines a voice on this one: a headless
devbox, a CI runner, an agent on your laptop, a home automation hub — anything on
the LAN sends text and it comes out of this computer's speakers.

**What it does beyond "say this":**

| | |
|---|---|
| **Priorities** | Four levels. An emergency jumps the queue and can cut off whatever is mid-sentence. |
| **Queue** | One player thread, so nothing ever talks over anything else. Stale announcements expire instead of piling up. |
| **Quiet hours** | Time windows where unimportant speech is deferred, dropped or spoken quietly — with an override level that always gets through. |
| **Backends with fallback** | Name an engine, or ask for `fastest` (measured, not configured) or `random`. Failures fall through, and a failing engine is rested. |
| **Voice cache** | Repeated phrases ("tests passed") replay from disk: instant, and no CPU spent saying them again. |
| **Audio routing** | Per-request output device, with friendly names so clients don't hard-code hardware. |
| **Auth & rate limits** | Named bearer tokens and a per-client token bucket. Both off until you set them; callers on this host stay exempt. |
| **Ways in** | HTTP, a shell client, MQTT, and templated webhook receivers for GitHub/Alertmanager/Grafana/Uptime Kuma. |
| **Dashboard** | Live queue with controls, engine health, and a searchable history you can replay. |

## Quick start

```sh
git clone <this repo> && cd speak-server
docker compose up -d

# locally
curl -sS -X POST --data "Testing." http://127.0.0.1:8899/speak

# from another machine on the LAN (use this host's address)
curl -sS -X POST --data "Testing." http://<this-host-ip>:8899/speak

# and open the dashboard
xdg-open http://127.0.0.1:8899/
```

That's the whole setup: kokoro only, default voice, no authentication, no quiet
hours. Everything else is opt-in through `.env` — copy `.env.example` and
uncomment what you want.

Use `127.0.0.1`, not `localhost` — the container binds IPv4 and `localhost` may
resolve to `::1`.

> **After changing anything under `speak-server/`, rebuild *and* recreate.**
> Neither `docker compose up -d` nor `podman compose up -d` rebuilds an image
> that already exists, and neither replaces a *running* container just because
> the image changed. The symptom is confusing: your change appears to have no
> effect, or — if the old code can't cope with new compose variables — the
> container crash-loops and the port resets the connection instead of refusing it.
>
> ```sh
> docker compose up -d --build --force-recreate speak-server
> # podman: podman compose build speak-server && podman compose up -d --force-recreate
> ```
>
> To check which code is actually running:
> `docker compose exec speak-server head -3 /app/audio.py`

> **A note on `data/`.** Cache, history and replay clips live in `./data`, bind-mounted
> into the container. It's in the repo (git-ignored) so it belongs to whoever
> cloned it — a named volume would be created root-owned, and the container runs
> as your uid. The mount carries `:Z` because on SELinux hosts
> (Fedora/RHEL/CentOS) writes are otherwise denied and the server quietly falls
> back to no cache and no history. If you move the directory, the mount and
> `DATA_DIR` have to agree.

## The dashboard

`http://<host>:8899/` — a playout rundown for the machine's voice.

- **Now speaking** — the sentence currently being said, with a real position line
  (elapsed against the clip's decoded length) and where it came from.
- **Next up** — everything queued, in the order it will actually be spoken, with
  per-item removal. Deferred items show the time they're being held until.
- **Say something** — a composer with engine, voice, priority and output pickers,
  for testing a voice without reaching for `curl`.
- **Machine state** — measured per-engine latency and failure state, quiet-hours
  status with a snooze button, cache hit rate, output routing, tokens and
  remaining rate-limit allowance, and whether MQTT and webhooks are live.
- **Said earlier** — searchable history. Every row can be played back in the
  browser (▶) or spoken again on the speakers (↺).

Transport controls in the header: **Mute** (`m`), **Skip** (`s`), **Stop
everything**. Muting *drops* what arrives rather than holding it, so unmuting
never unleashes a backlog.

It follows the same auth as everything else. Browsers can't set an
`Authorization` header on a plain navigation, so open it once as
`http://host:8899/?token=<your-token>`; the server swaps that for a cookie and
the page cleans the token out of the address bar. Set `DASHBOARD=0` to serve the
API only.

## Speaking

The body is plain text, or JSON:

```sh
# plain text
curl -sS -X POST --data "Build finished, all tests green." http://127.0.0.1:8899/speak

# JSON with overrides
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"text": "Hello.", "voice": "af_bella", "speed": 1.2}' \
  http://127.0.0.1:8899/speak

# query-string overrides, so one-liners need no jq
curl -sS -X POST --data "Deploy failed." 'http://127.0.0.1:8899/speak?priority=high'

# don't wait for the audio to finish
curl -sS -X POST --data "Long report..." 'http://127.0.0.1:8899/speak?wait=false'
```

### Fields

| Field | Default | Meaning |
|---|---|---|
| `text` | — | Required. Up to `MAX_TEXT` (5000) characters. |
| `engine` | `ENGINE` | `kokoro`, `supertonic`, `fastest`, `random`. |
| `voice` | per engine | Engine-specific. Don't set it without the matching `engine`. |
| `speed` | `1.0` | 0.25–4.0. |
| `lang` | auto | Supertonic only (`ko`, `ja`, …). |
| `priority` | `DEFAULT_PRIORITY` | `emergency`, `high`, `normal`, `low`. |
| `sink` | `AUDIO_SINK` | Output device, or a name from `AUDIO_ROUTES`. |
| `volume` | `VOLUME` | 0–100. Quiet hours may lower it further. |
| `wait` | `true` | `false` returns as soon as it's queued. |

Any of them can also be passed in the query string, which is what makes the
plain-text form useful.

### Responses

`POST /speak` blocks until playback finishes by default, and **2xx means it was
really spoken**:

| Code | Meaning |
|---|---|
| `200 spoke` | Synthesized and played to completion. |
| `202` | Queued (`wait=false`), or deferred to the end of quiet hours. |
| `400` | Bad request — no text, unknown engine, out-of-range speed. |
| `401` | A token is required, or the one sent is wrong. |
| `409` | Deliberately not spoken: quiet hours dropped it, playback is muted, it was cut off, or it expired. |
| `413` | Text longer than `MAX_TEXT`. |
| `429` | Rate limited. `Retry-After` says how long. |
| `500` | Synthesis worked, playback failed — usually no desktop audio session. |
| `502` | Synthesis failed. The body lists what each engine tried and said. |
| `503` | Queue full of things at least as important. |
| `504` | Still not spoken after synthesis + playback + queue timeouts. |

## Priorities, the queue, and interruption

Playback is serialized by a single player thread, so overlapping requests queue
instead of talking over each other. Order is **(priority, arrival)**.

Nothing ages up — a `low` item never overtakes a `normal` one, because these are
announcements and an old one is usually *less* interesting than a new one. What
stops `low` from starving is `QUEUE_ITEM_TTL` (default 300s): it expires rather
than waiting forever behind more important speech. "Build started" is noise once
the build has finished.

**Interruption.** By default a request that outranks what's playing by one level
or more cuts it off (`INTERRUPT=1`, `INTERRUPT_MIN_GAP=1`). So `normal` will chop
off `low` mid-sentence — which is the point of marking something `low`, but worth
knowing before you label your chatty notifier that way. Options:

- `INTERRUPT_MIN_GAP=2` — only `emergency` interrupts `normal`, only `high`
  interrupts `low`.
- `INTERRUPT=0` — never interrupt; important speech still jumps the queue.
- `INTERRUPT_REQUEUE=1` — the interrupted utterance is spoken again afterwards
  instead of being dropped (capped at three replays, so two clients trading
  alerts can't restart the same clip forever).

**A full queue sheds its least important item** rather than refusing the newest.
An emergency arriving into a queue of a hundred `low` items must not be the one
that gets rejected. If nothing in the queue is less important than the newcomer,
the newcomer gets the `503`.

```sh
# jumps the queue and cuts off whatever is talking
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"text": "The server room is on fire.", "priority": "emergency"}' \
  http://127.0.0.1:8899/speak
```

## Quiet hours

```sh
# .env
QUIET_HOURS=23:00-07:30        # may wrap midnight; comma-separate several
QUIET_HOURS_TZ=Europe/London   # falls back to TZ
QUIET_HOURS_POLICY=defer       # defer | drop | attenuate
QUIET_HOURS_OVERRIDE=emergency # at or above this, quiet hours don't apply
QUIET_HOURS_VOLUME=25          # used by attenuate
```

Inside a window, anything less important than `QUIET_HOURS_OVERRIDE` is:

- **`defer`** — held and spoken when the window ends. Its TTL is pushed past the
  window too, so deferring can't quietly become dropping. A caller waiting
  synchronously gets `202` immediately rather than being held until morning.
- **`drop`** — thrown away, and the caller is told (`409`).
- **`attenuate`** — spoken now at `QUIET_HOURS_VOLUME`, taking the *quieter* of
  that and whatever the request asked for. A client requesting volume 100 at 3am
  is exactly the case this exists to prevent.

The window is re-checked when an utterance reaches the front of the queue, not
just when it arrives — something queued at 22:59 and reached at 23:01 is
governed by what would actually be heard.

Watching a film? **Snooze** it from the dashboard, or:

```sh
curl -sS -X POST 'http://127.0.0.1:8899/api/quiet/snooze?seconds=7200'
curl -sS -X POST http://127.0.0.1:8899/api/quiet/resume
```

Snoozing also **releases whatever the window already deferred**, for the same
reason the window is re-checked at the front of the queue: a deferral is a
condition, not a deadline. Holding a backlog until the original window end after
the window has been snoozed away would silence exactly the announcements the
snooze was meant to let through. `resume` puts the window back, and anything
still queued is deferred again.

Note the argument is `seconds`; an unrecognised one (`?minutes=5`) is ignored and
you get an indefinite snooze, which `resume` is the way out of.

An unparseable `QUIET_HOURS` is logged and ignored rather than taken as
midnight-to-midnight, which would silence the server completely.

## Choosing engines

By default only kokoro runs. All engine choices live in `.env` next to the
compose file (copy `.env.example`):

```sh
COMPOSE_PROFILES=supertonic   # also run the supertonic container
ENGINE=supertonic             # engine used when a request doesn't name one
VOICE=af_heart                # default kokoro voice
SUPERTONIC_VOICE=M1           # default supertonic voice
```

`ENGINE` (and the per-request `engine`) accepts two selectors as well as a name:

- **`fastest`** — orders local engines by *measured* median synthesis time over
  the last `LATENCY_WINDOW` (10) runs, and falls through on failure. Engines with
  no measurement yet go first, so everything gets sampled before the ranking
  settles. This is measured rather than configured because which engine is
  quicker depends on the host and the load, not on which one looks faster.
- **`random`** — a random engine per request, falling through on failure, so
  turning an engine off later costs variety and never speech.

**A request that *names* an engine gets no peer fallback** — the caller asked for
that one, and a stand-in voice would misreport what happened.

**Failing engines are rested.** After `ENGINE_FAILURE_THRESHOLD` (3) consecutive
failures an engine is pushed to the back of automatic selection for
`ENGINE_COOLDOWN` (60s). Pushed back, not removed — a total outage should still
produce a real error from a real attempt rather than "no engines available". One
success clears it.

Voices, without publishing engine ports:

```sh
curl -sS http://127.0.0.1:8899/api/voices          # every engine, via speak-server
curl -sS http://127.0.0.1:8899/api/voices/kokoro
```

Prefer supertonic? `COMPOSE_PROFILES=supertonic` plus `ENGINE=supertonic` makes
it the default. kokoro still runs (it's cheap when idle); to drop it entirely,
remove both the `kokoro` service and speak-server's `depends_on` on it in
`docker-compose.yml`.

The supertonic container installs only the SDK; the ~400 MB of model assets live
in `supertonic/models/supertonic-3` (git-ignored, bind-mounted read-only;
override with `SUPERTONIC_MODEL_DIR`). On a fresh clone, seed them once:

```sh
pip install supertonic
SUPERTONIC_CACHE_DIR=./supertonic/models/supertonic-3 supertonic download
```

Supertonic also takes an optional `lang` code (`"ko"`, `"ja"`, `"de"`, … —
31 languages; `supertonic tts --help` lists them). Omitted, it uses the `na`
fallback, which copes with unknown or mixed-language text; an explicit code
pronounces better. It's forwarded only to supertonic — the other engines would
reject the unknown field, and kokoro picks language from the voice prefix
(`af_*` American female, `bf_*` British female, …).

## Voice cache

Synthesis is the slow part — seconds of CPU for a sentence.
The phrases a notifier actually says repeat constantly, so they're cached on disk
under `data/cache` and a repeat becomes a file read.

The key covers engine, model, voice, speed, language, text **and** the lead
silence — so a hit is byte-identical to what a fresh synthesis would have
produced, and `fastest`/`random` can reuse whatever an earlier pick left behind
without ever playing the wrong voice.

Text longer than `CACHE_MAX_TEXT` (400 chars) is deliberately never cached: a
paragraph of build output is never asked for twice, and it would evict the short
phrases that are. Eviction is LRU by access time, bounded by `CACHE_MAX_MB` (512)
and `CACHE_MAX_AGE_DAYS` (30), swept every 15 minutes. `CACHE_ENABLED=0` turns it
off; the dashboard shows the hit rate and can empty it.

## Audio routing

```sh
# .env
AUDIO_SINK=                                  # blank = the session default sink
AUDIO_ROUTES=desk=alsa_output.pci-0000_00_1f.3.analog-stereo,tv=alsa_output.hdmi-stereo
VOLUME=100
```

Then clients name a route rather than hardware:

```sh
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"text": "Dinner is ready.", "sink": "kitchen", "volume": 80}' \
  http://127.0.0.1:8899/speak
```

Aliases win over raw device names, so a route can be repointed at new hardware
without touching a single client. List what's available:

```sh
curl -sS http://127.0.0.1:8899/api/sinks
```

`LEAD_SILENCE_MS` (500) prepends silence to every clip. The audio sink suspends
when idle and spends the first few hundred milliseconds resuming, which
otherwise clips the first syllable; the silence absorbs the ramp instead. Set `0`
to disable.

**Volume is per utterance and deliberately doesn't stick.** PulseAudio and
PipeWire both run `module-stream-restore`, which remembers a volume per
*application name* and reapplies it to every new stream from that application.
Every clip here shares one client name, so a single request with `"volume": 20`
would otherwise teach the sound server that speak-server plays at 20% — and every
later utterance, including ones asking for full volume, would inherit it and be
inaudible. The server therefore states the volume explicitly on every clip rather
than only when it differs from the default. If you want a persistent change, set
`VOLUME` in `.env`; the per-request field is for one-offs.

If speech is inexplicably silent while `/speak` returns `200 spoke`, that
mechanism is the first thing to check — the exit code is honest, the audio really
was streamed, just at a volume you can't hear:

```sh
# during playback
pactl list sink-inputs | grep -A1 'speak-server'
```

## Authentication and rate limits

Both are off until you configure them, and **setting tokens is the only switch** —
there's no second flag to forget:

```sh
# .env
SPEAK_TOKENS=laptop=Xk9...,ci=Qp2...,hass=Rt7...
RATE_LIMIT=60/60                    # 60 requests, refilling over 60s
AUTH_EXEMPT_CIDRS=127.0.0.0/8,::1/128   # optional; see below before setting it
```

Generate one with `openssl rand -base64 24`.

Tokens are **named**, so history and the dashboard show *which* client spoke and
one client can be revoked without rotating everyone. Send it as
`Authorization: Bearer <token>`, `X-Speak-Token: <token>`, or `?token=` (which
the dashboard swaps for a cookie).

**Callers on this host are exempt by default.** A shell on the host is already
inside the trust boundary — it could run `paplay` directly — so requiring a token
there only breaks the local client and teaches people to disable auth entirely.

Exempting loopback is not enough to achieve that in a container, which is worth
knowing if you ever pin the list yourself. Published ports are NAT'd: a request
from a shell on this host reaches the container from the compose bridge gateway,
so `127.0.0.1` never appears as a client address and a loopback-only rule never
fires. The server detects its gateway at startup and exempts that too, and says
so in the log — the address is whatever compose assigned this project's bridge,
so read it there rather than copying one from here:

```
auth exempt: 127.0.0.0/8, ::1/128, 172.26.0.1/32
auth: 172.26.0.1 is this container's gateway, so callers on the host (and
anything else reaching the published port through it) skip auth
```

That parenthesis is the trade: another container on this host reaching the
published port shares that gateway and is exempt too. Remote callers keep their
own source address and still need a token. Setting `AUTH_EXEMPT_CIDRS` yourself
replaces the whole list and is used verbatim — the gateway is *not* appended, so
include it (with the address from your own log) if you still want local callers
exempt. To require a token even
locally, set `AUTH_EXEMPT_CIDRS=none` — a word rather than an empty value,
because an empty value means "unset, use the default" for every variable here and
so couldn't express this.

`GET /health` never requires a token: a monitoring probe shouldn't need a
credential, and it reveals nothing beyond "this process is alive".

The rate limit is a **token bucket**, not a fixed window: notifiers are bursty by
nature — a CI run finishing fires four announcements in a second, then nothing
for an hour — and a bucket absorbs that while still catching sustained abuse.
It's per identity, so one noisy client can't silence another.
`RATE_LIMIT_EXEMPT_PRIORITY=emergency` means emergencies bypass it entirely: a
rate limit that silences an alarm has done more damage than the abuse it was
protecting against.

## Ways in

### Shell client

`examples/speak.sh` — JSON escaping via `jq`, stdin support, a timeout sized for
synthesis-plus-playback, and error messages that distinguish "container not
running" from "up but not responding". Needs `curl` and `jq`.

```sh
examples/speak.sh "All tests passed."
some-command | examples/speak.sh
PRIORITY=high SINK=desk examples/speak.sh "Deploy failed."
SPEAK_HOST=nas.local:8899 SPEAK_TOKEN=Xk9... examples/speak.sh "Backup done."
```

Env: `SPEAK_HOST`, `SPEAK_TOKEN`, `ENGINE`, `VOICE`, `MODEL`, `SPEED`,
`LANG_CODE`, `PRIORITY`, `SINK`, `VOLUME`, `NOWAIT`. Anything unset isn't sent,
so the server's defaults apply and the client keeps working whatever engines that
server has.

### MQTT

Home automation is where this earns its keep: Home Assistant, Node-RED, an ESP32
button and most hub software can publish an MQTT message far more easily than
they can make an authenticated HTTP POST.

```sh
# .env
MQTT_HOST=192.168.1.10
MQTT_USERNAME=speak
MQTT_PASSWORD=...
```

| Topic | Direction | Payload |
|---|---|---|
| `speak/say` | in | Plain text, or the same JSON body as `POST /speak`. |
| `speak/say/<priority>` | in | Text, with the priority taken from the topic. |
| `speak/cmd/<action>` | in | `stop`, `skip`, `clear`, `mute`, `unmute`. |
| `speak/status` | out | Retained snapshot: playing, queue depth, muted, quiet hours. |
| `speak/status/online` | out | Retained `true`/`false`, with a last will — so subscribers can tell "quiet" from "gone". |
| `speak/event` | out | One message per state change, with the full utterance record. |

```sh
mosquitto_pub -h 192.168.1.10 -t speak/say -m "The back door is open."
mosquitto_pub -h 192.168.1.10 -t speak/say/emergency -m "Smoke detected upstairs."
mosquitto_pub -h 192.168.1.10 -t speak/cmd/mute -m ""
```

MQTT is fire-and-forget: there's nobody holding a response open, so nothing
published ever blocks waiting for playback.

Everything about it is non-fatal. No `MQTT_HOST` and the bridge does nothing;
`paho-mqtt` missing and it logs a warning and carries on; broker down and paho
reconnects on its own. The HTTP server must never fail to start because a broker
is unreachable. (Build with `--build-arg WITH_MQTT=0` for a dependency-free
image.)

### Webhooks

`POST /speak` already takes JSON, but the things you want to hear from — GitHub,
Alertmanager, Grafana, Uptime Kuma — send *their* schema and can't be talked out
of it. A receiver is a named endpoint plus a template that pulls fields out of
whatever arrives.

Copy `examples/webhooks.json` to `data/webhooks.json`:

```json
{
  "ci":     { "preset": "github",       "priority": "low",  "secret": "..." },
  "alerts": { "preset": "alertmanager", "priority": "high",
              "match": { "status": "firing" } },
  "hass":   { "template": "{message}",  "sink": "kitchen" }
}
```

Each key becomes `POST /webhook/<key>`. Presets exist for `github`,
`alertmanager`, `grafana`, `uptimekuma` and `generic` (which speaks the first of
`text`/`message`/`msg`/`body`/`summary`/… it finds).

`{dotted.path}` pulls from the payload and digits index into lists
(`{alerts.0.labels.instance}`). A path the payload doesn't have renders as
nothing **and its leftover punctuation is tidied away**, which is what lets one
template cover several event types instead of needing one per event.

- `match` filters, so resolved alerts don't wake anyone up. Values compare as
  text, because senders are inconsistent about quoting numbers.
- `secret` is checked from `?secret=` or `X-Webhook-Secret`, and stands in for the
  normal token — most senders can't set an `Authorization` header. Without one,
  normal auth applies.
- `max_length` (300) truncates at a word boundary. Webhook payloads are
  machine-generated and can be enormous; unlike text a person typed, truncating
  is kinder than refusing.

Reload after editing, without a restart:

```sh
curl -sS -X POST http://127.0.0.1:8899/api/webhooks/reload
```

## API reference

| Method | Path | |
|---|---|---|
| `POST` | `/speak` | Speak text. Also `/api/speak`. |
| `GET` | `/health` | `200 ok`. No token needed. `?verbose=1` for JSON detail. |
| `GET` | `/` | Dashboard. |
| `GET` | `/api/status` | Everything: queue, engines, quiet hours, cache, history, auth, audio, MQTT, webhooks. |
| `GET` | `/api/queue` | What's playing and what's waiting. |
| `DELETE` | `/api/queue/<id>` | Remove one item, or stop it if it's the one playing. |
| `POST` | `/api/queue/clear` | Drop everything waiting; leaves playback alone. |
| `POST` | `/api/skip` | End the current utterance, move on. |
| `POST` | `/api/stop` | Stop playback *and* clear the queue — the panic button. |
| `POST` | `/api/mute` | `{"muted": true}`, or omit the body to toggle. |
| `POST` | `/api/quiet/snooze` | `?seconds=3600`, or no argument for indefinitely. |
| `POST` | `/api/quiet/resume` | Re-enable quiet hours. |
| `GET` | `/api/history` | `?limit=&offset=&status=&q=&source=` |
| `GET` | `/api/history/<id>` | One row. |
| `GET` | `/api/history/<id>/audio` | The WAV, for replay in a browser. |
| `POST` | `/api/history/<id>/replay` | Say it again on the speakers. |
| `GET` | `/api/voices`, `/api/voices/<engine>` | What each engine offers. |
| `GET` | `/api/sinks` | Output devices and configured routes. |
| `GET` | `/api/webhooks` | Configured receivers. |
| `POST` | `/api/webhooks/reload` | Re-read the receiver file. |
| `POST` | `/api/cache/clear` | Empty the voice cache. |
| `POST` | `/webhook/<name>` | A configured receiver. |

Endpoints under `/api/` always answer JSON. `/speak` answers a line of plain text
so `curl` and shell scripts stay readable, unless you send
`Accept: application/json`.

## History

Every utterance is logged to `data/history.db` (SQLite) once it reaches a
terminal state — `spoke`, `failed`, `interrupted`, `dropped`, `expired`,
`cancelled`, `muted` — with its client, engine, timings, and whether it was a
cache hit. Rows are written once and never updated, so there's no window where a
row is half-true; in-flight work is visible from the queue instead.

Audio for replay is usually just the cache entry, referenced in place. When the
cache wouldn't have kept it (text too long, caching off), `HISTORY_KEEP_AUDIO`
puts a copy in `data/clips` so the dashboard's play button works for every row
rather than mysteriously only some. Retention: `HISTORY_MAX_ROWS` (5000),
`HISTORY_MAX_AGE_DAYS` (30), `HISTORY_AUDIO_MAX_MB` (256).

It's an ordinary SQLite file, so:

```sh
sqlite3 data/history.db \
  "SELECT datetime(created_at,'unixepoch','localtime'), client, status, text
   FROM utterances ORDER BY id DESC LIMIT 20;"
```

Replay deliberately re-runs the whole pipeline rather than pushing the stored
clip at the speakers: the cache makes it instant anyway, and this way a replay
honours the *current* quiet hours, mute state and routing instead of bypassing
them.

## Running the container

The container must run as the desktop user who owns the audio session. This
defaults to uid/gid 1000; if yours differ (`id -u`), export `UID` and `GID` (or
set them in `.env`) before `up`. The Pulse cookie is mounted from
`~/.config/pulse/cookie`.

### Podman

The stack runs under `podman-compose` unchanged except for one thing: rootless
podman maps container uid 1000 to a *subuid* on the host, so the container user
isn't you and can't read the `0600` Pulse cookie — synthesis succeeds, playback
fails to authenticate. Set `USERNS_MODE=keep-id` in `.env`:

```sh
echo 'USERNS_MODE=keep-id' >> .env
podman-compose up -d
```

Leave `USERNS_MODE` unset under Docker — it only accepts `host` there, and the
default (empty) is already correct.

Two more podman details:

- `restart: always` needs `podman-restart.service` enabled to survive a reboot;
  Docker handles it via the daemon.
- Podman builds OCI images by default, and **OCI has no `HEALTHCHECK`** — it's
  dropped with a warning, so `restart: always` will only act on a crashed
  process, not a wedged one. Build with `podman build --format docker` (or run
  `podman healthcheck run speak-server` from a timer) if you want it.

### SELinux

On Fedora, RHEL and CentOS, SELinux blocks a container from writing to the host's
PulseAudio socket. Synthesis succeeds, playback fails, and the error looks like a
missing server rather than a permission problem:

```
paplay failed (1): Connection failure: Connection refused
pa_context_connect() failed: Connection refused
```

The audit log is where it actually says so:

```sh
sudo ausearch -m avc -ts recent | grep -E 'paplay|pactl'
# avc: denied { write } for comm="paplay" name="native"
#   scontext=…:container_t:s0  tcontext=…:user_tmp_t:s0  tclass=sock_file
```

`container_t` may not write a `user_tmp_t` socket. Relabelling the socket file
doesn't hold — PipeWire recreates it on restart and the label goes with it —
so the compose file opts this one container out of SELinux confinement:

```yaml
security_opt:
  - label=disable
```

It's ignored on hosts without SELinux and by Docker when SELinux isn't enabled,
so it costs nothing elsewhere. This container already has full access to your
audio session by design, so it widens the blast radius less than it appears —
but if you'd rather keep it confined, install a targeted policy module instead
and drop the `security_opt` block:

```sh
cat > speak-audio.te <<'EOF'
module speak-audio 1.0;
require {
    type container_t;
    type user_tmp_t;
    class sock_file write;
    class unix_stream_socket connectto;
}
# Let containers talk to the desktop audio socket, and nothing else new.
allow container_t user_tmp_t:sock_file write;
allow container_t self:unix_stream_socket connectto;
EOF
checkmodule -M -m -o speak-audio.mod speak-audio.te
semodule_package -o speak-audio.pp -m speak-audio.mod
sudo semodule -i speak-audio.pp     # remove later: sudo semodule -r speak-audio
```

That needs `checkpolicy` and `policycoreutils-devel` installed. The policy route
is narrower but applies host-wide to every `container_t`; `label=disable` is
broader but scoped to this one container. Neither is obviously better — pick the
boundary you'd rather keep.

### The mount dance (why the volumes look weird)

Playing audio from a container requires the host's Pulse socket, and the naive
bind mount breaks in two ways:

1. **Mounting the socket file** (`.../pulse/native`) strands a stale inode when
   Pulse restarts on the host.
2. **Mounting the socket directory** (`/run/user/1000/pulse`) survives Pulse
   restarts but loses to a **reboot race**: `/run/user/<uid>` is a tmpfs that
   systemd-logind mounts *at login*. A container autostarted at boot binds a
   placeholder directory before that tmpfs exists, and with Docker's default
   `rprivate` propagation the real mount never appears inside — playback fails
   with `Connection refused` until the container is restarted.

The fix used here: bind `/run/user` (which exists in the host's `/run` tmpfs
regardless of boot order) with `bind.propagation: rslave`. The login-time tmpfs
mount then propagates into the already-running container, so the stack survives
both reboots and Pulse restarts with `restart: always`.

## Running unattended

The things that make it survive months without attention:

- **Nothing optional is fatal.** An unwritable `DATA_DIR` disables cache and
  history and keeps speaking. A missing `paho-mqtt`, an unreachable broker, a
  malformed `webhooks.json`, an unknown timezone, an unparseable `QUIET_HOURS` —
  all logged and stepped over. Only genuinely contradictory configuration
  (`AUTH_REQUIRED` with no tokens, a volume outside 0–100) refuses to start, and
  it says exactly what's wrong.
- **Bounded everything.** Queue depth, item TTL, text length, request body size,
  cache size and age, history rows and age, clip directory size, synthesis and
  playback timeouts. Nothing grows without a limit, so nothing fills the disk.
- **Housekeeping off the hot path.** Retention sweeps and rate-limit bucket
  expiry run on a 15-minute timer, never in the path of something a person is
  waiting to hear. A failing sweep logs and retries next pass.
- **Engine cooldowns**, so a dead backend stops being chosen instead of being
  retried into every request.
- **A real healthcheck** in the image (stdlib, no curl needed), so
  `restart: always` acts on a wedged process rather than only a crashed one.
- **Clean shutdown** on SIGTERM: the player thread stops first, so `compose down`
  doesn't wait out a paragraph someone is having read to them.
- **Atomic cache writes** (write-then-rename), so a crash mid-write can't leave a
  truncated WAV that later plays as a burst of noise.
- **Torn-off clients are normal**, not errors — a long blocking `/speak` gets
  hung up on routinely, and the server doesn't log a stack trace over it.

Watch it with `docker compose logs -f speak-server`, the dashboard, or:

```sh
curl -sS 'http://127.0.0.1:8899/health?verbose=1'
```

## Claude Code skill

`examples/claude-code-skill/` is a [Claude Code](https://claude.com/claude-code)
skill giving the agent a `/speak` command — including instructions for rewriting
identifiers, ticket numbers and IDs so the TTS pronounces them like a human
would. Copy the directory to `~/.claude/skills/speak/` along with `speak.sh`:

```sh
mkdir -p ~/.claude/skills/speak
cp examples/claude-code-skill/SKILL.md examples/speak.sh ~/.claude/skills/speak/
```

## Configuration

Every variable may be left blank: compose always passes it through, so the server
reads an empty string as "unset" and applies its own default. See `.env.example`
for the annotated list.

| | |
|---|---|
| **Engines** | `ENGINE` `VOICE` `SUPERTONIC_VOICE` `KOKORO_URL` `SUPERTONIC_URL` `SYNTH_TIMEOUT` `LATENCY_WINDOW` `ENGINE_FAILURE_THRESHOLD` `ENGINE_COOLDOWN` |
| **Queue** | `DEFAULT_PRIORITY` `QUEUE_MAX` `QUEUE_ITEM_TTL` `INTERRUPT` `INTERRUPT_MIN_GAP` `INTERRUPT_REQUEUE` `MAX_TEXT` |
| **Quiet hours** | `QUIET_HOURS` `QUIET_HOURS_TZ` `QUIET_HOURS_POLICY` `QUIET_HOURS_OVERRIDE` `QUIET_HOURS_VOLUME` |
| **Audio** | `AUDIO_SINK` `AUDIO_ROUTES` `VOLUME` `LEAD_SILENCE_MS` `PLAY_TIMEOUT` |
| **Cache** | `CACHE_ENABLED` `CACHE_MAX_MB` `CACHE_MAX_AGE_DAYS` `CACHE_MAX_TEXT` `VOICE_LIST_TTL` |
| **History** | `HISTORY_ENABLED` `HISTORY_MAX_ROWS` `HISTORY_MAX_AGE_DAYS` `HISTORY_KEEP_AUDIO` `HISTORY_AUDIO_MAX_MB` |
| **Access** | `SPEAK_TOKENS` `SPEAK_TOKEN` `AUTH_REQUIRED` `AUTH_EXEMPT_CIDRS` `RATE_LIMIT` `RATE_LIMIT_EXEMPT_PRIORITY` `DASHBOARD` |
| **MQTT** | `MQTT_HOST` `MQTT_PORT` `MQTT_USERNAME` `MQTT_PASSWORD` `MQTT_TLS` `MQTT_CLIENT_ID` `MQTT_TOPIC` `MQTT_STATUS_TOPIC` `MQTT_EVENT_TOPIC` `MQTT_QOS` |
| **Other** | `PORT` `BIND` `DATA_DIR` `WEBHOOKS_FILE` `LOG_LEVEL` `TZ` |

## Security notes

Port `8899` is published on **all interfaces**, because the whole point is
letting other machines speak here. Anyone who can reach it can make this machine
talk, and — with the dashboard — read everything it has ever said.

- **Set `SPEAK_TOKENS`** if the LAN isn't fully trusted. That alone turns on
  authentication for the API, the dashboard and every webhook without a secret.
- **Narrow the binding** with `SPEAK_BIND=127.0.0.1` (then reach it over an SSH
  tunnel) or a specific LAN address, and/or restrict `8899` in your firewall.
- **There is no TLS.** Tokens cross the network in the clear, so on anything
  beyond a trusted LAN put it behind a reverse proxy that terminates TLS.
- **Webhook receivers should carry a `secret`**, since a webhook sender usually
  can't hold a token.
- **Rate limits are abuse control, not authorization.** They stop a runaway
  script, not someone who wants in.

TTS engines are not published to the host, so synthesis CPU stays internal.

The mounted Pulse cookie and socket give the container full access to your audio
session (including capture, in principle). The mounts are read-only and the
server only ever spawns `paplay` and `pactl`, but treat the container as trusted.

`data/` holds the text of everything spoken, plus WAVs of it. If any of that is
sensitive, set `HISTORY_ENABLED=0` (or `HISTORY_KEEP_AUDIO=0` to keep the log but
not the recordings).

## Development

The server is a flat set of modules under `speak-server/`, stdlib only apart from
the optional `paho-mqtt`:

| | |
|---|---|
| `server.py` | Entrypoint; wires everything together and keeps the process alive. |
| `config.py` | All environment parsing, in one place. Empty means unset. |
| `engines.py` | The TTS backends, health, and measured latency. |
| `cache.py` | Content-addressed audio cache, plus a TTL cache of voice lists. |
| `audio.py` | Sink routing, volume, interruptible playback. |
| `quiethours.py` | Time windows and what to do inside them. |
| `queues.py` | Priority queue, the player thread, and the synthesis pipeline. |
| `history.py` | SQLite log and the audio for replay. |
| `auth.py` | Tokens and rate limits. |
| `api.py` | HTTP surface and dashboard serving. |
| `webhooks.py` | Templated receivers for other people's JSON. |
| `mqtt.py` | Optional broker bridge. |
| `dashboard/` | One self-contained HTML file. No build step, no dependencies. |

Tests are stdlib `unittest`, no test dependencies, and touch neither a real
engine nor the audio session:

```sh
python3 tests/test_speak.py
```

## License

MIT — see [LICENSE](LICENSE).
