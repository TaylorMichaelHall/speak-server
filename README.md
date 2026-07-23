# kokoro-tts + speak-server

Give the agents (or anything else) on your machine a voice. A compose stack:

- **kokoro** — [Kokoro FastAPI](https://github.com/remsky/Kokoro-FastAPI)
  (CPU image), an OpenAI-compatible TTS server on port **8880**.
- **speak-server** — a ~150-line Python HTTP server on port **8899**. POST it
  text; it synthesizes via the chosen engine and plays the audio **on the
  host's speakers** through the mounted PulseAudio/PipeWire socket. The
  caller just POSTs text — no audio handling client-side.
- **supertonic** (opt-in, off by default) —
  [Supertonic](https://github.com/supertone-inc/supertonic)
  (`supertonic serve`), an alternative OpenAI-compatible TTS engine on port
  **7788** with voices `M1`–`M5` and `F1`–`F5`. Enable it with one line in
  `.env` (see [Choosing engines](#choosing-engines)).

The point is to give tools on **other** machines a voice on this one: a
headless devbox, a CI runner, an agent on your laptop — anything on the LAN
POSTs text and it comes out of this computer's speakers. speak-server is
published on all interfaces for exactly that; see [Security](#security-notes).

Playback is serialized (single-threaded server), so overlapping requests
queue instead of talking over each other. Errors map to non-2xx so callers
never believe they spoke when nothing played.

## Usage

```sh
# plain text body
curl -sS -X POST --data "Build finished, all tests green." http://127.0.0.1:8899/speak

# or JSON with engine/voice/speed overrides
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"text": "Hello.", "voice": "af_bella", "speed": 1.2}' \
  http://127.0.0.1:8899/speak

# speak through supertonic instead of kokoro
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"text": "Hello.", "engine": "supertonic", "voice": "F2"}' \
  http://127.0.0.1:8899/speak
```

`engine` is `kokoro` (default), `supertonic`, or `random` — see
[Choosing engines](#choosing-engines). When `voice` is omitted, each
engine gets its own default (`af_heart` / `M1`) — voice names aren't shared
between engines, so don't set one without the other.

Supertonic also takes an optional `lang` code (`"ko"`, `"ja"`, `"de"`, … —
31 languages; run `supertonic tts --help` for the list). Omitted, it uses the
`na` fallback, which copes with unknown or mixed-language text; an explicit
code pronounces better. Kokoro instead selects language by voice prefix
(`af_*` American female, `bf_*` British female, …).

- `GET /health` → `200 ok`
- `POST /speak` → `200 spoke` after playback finishes (the request **blocks
  until the audio is done playing**), `400` empty text or unknown engine,
  `502` synthesis failed, `500` synthesis worked but playback failed
  (usually: no desktop audio session).
- List kokoro voices: `curl -s http://127.0.0.1:8880/v1/audio/voices`
- List supertonic voices: `curl -s http://127.0.0.1:7788/v1/styles`

Use `127.0.0.1`, not `localhost` — the containers bind IPv4 and `localhost`
may resolve to `::1`.

## Setup

```sh
docker compose up -d
# locally
curl -sS -X POST --data "Testing." http://127.0.0.1:8899/speak
# from another machine on the LAN (use this host's address)
curl -sS -X POST --data "Testing." http://<this-host-ip>:8899/speak
```

speak-server binds `8899` on all interfaces by default, so LAN clients can
reach it out of the box. kokoro (`8880`) and supertonic (`7788`) stay on
loopback — nothing external needs them.

## Choosing engines

By default only kokoro runs. All engine choices live in `.env` next to the
compose file (copy `.env.example`):

```sh
# .env
COMPOSE_PROFILES=supertonic   # also run the supertonic container
ENGINE=supertonic             # engine used when a request doesn't name one
                              # (kokoro [default], supertonic, or random)
VOICE=af_heart                # default kokoro voice
SUPERTONIC_VOICE=M1           # default supertonic voice
```

`ENGINE=random` picks an engine per request and falls through to the next
one if the pick fails, so turning an engine off later costs variety, never
speech. A request that *names* an engine gets no fallback — the caller asked
for that one, and a stand-in voice would misreport what happened.

Prefer supertonic? `COMPOSE_PROFILES=supertonic` plus `ENGINE=supertonic`
makes it the default for every request that doesn't name an engine. kokoro
still runs (it's cheap when idle); to drop it entirely, remove both the
`kokoro` service and speak-server's `depends_on` on it in
`docker-compose.yml` — requests naming `kokoro` then get a `502`.

The supertonic container installs only the SDK; the ~400 MB of model assets
live in `supertonic/models/supertonic-3` (git-ignored, bind-mounted
read-only; override the location with `SUPERTONIC_MODEL_DIR` in `.env`). On
a fresh clone, seed them once:

```sh
pip install supertonic
SUPERTONIC_CACHE_DIR=./supertonic/models/supertonic-3 supertonic download
```

The container must run as the desktop user who owns the audio session. This
defaults to uid/gid 1000; if yours differ (`id -u`), export `UID` and `GID`
(or set them in a `.env` file next to the compose file) before `up`. The
Pulse cookie is mounted from `~/.config/pulse/cookie`.

Env knobs on speak-server: `ENGINE` (default `kokoro`) — the engine used when
a request doesn't name one; `VOICE` (kokoro default, `af_heart`);
`SUPERTONIC_VOICE` (supertonic default, `M1`); `PORT`, `KOKORO_URL`,
`SUPERTONIC_URL`, `LEAD_SILENCE_MS` (default `500`) — silence prepended to each clip so the
audio sink's resume-from-idle ramp doesn't clip the first syllable; set `0` to
disable.

## The mount dance (why the volumes look weird)

Playing audio from a container requires the host's Pulse socket, and the
naive bind mount breaks in two ways:

1. **Mounting the socket file** (`.../pulse/native`) strands a stale inode
   when Pulse restarts on the host.
2. **Mounting the socket directory** (`/run/user/1000/pulse`) survives Pulse
   restarts but loses to a **reboot race**: `/run/user/<uid>` is a tmpfs that
   systemd-logind mounts *at login*. A container autostarted at boot binds a
   placeholder directory before that tmpfs exists, and with Docker's default
   `rprivate` propagation the real mount never appears inside — playback
   fails with `Connection refused` until the container is restarted.

The fix used here: bind `/run/user` (which exists in the host's `/run` tmpfs
regardless of boot order) with `bind.propagation: rslave`. The login-time
tmpfs mount then propagates into the already-running container, so the stack
survives both reboots and Pulse restarts with `restart: always`.

## Client & Claude Code skill

`examples/speak.sh` is a small client wrapper: JSON escaping via `jq`, stdin
support, a 300s timeout sized for synthesis-plus-playback, and precise error
messages that distinguish "container not running" from "up but not
responding". Needs `curl` and `jq`.

`examples/claude-code-skill/` is a [Claude Code](https://claude.com/claude-code)
skill that gives the agent a `/speak` command — including instructions for
rewriting identifiers, ticket numbers, and IDs so the TTS pronounces them
like a human would. Copy the directory to `~/.claude/skills/speak/` along
with `speak.sh` to install it.

## Security notes

speak-server (`8899`) is published on **all interfaces** and has **no
authentication** — that's deliberate, since the whole point is letting other
machines speak here. Anyone who can reach the port can make this machine talk.
Run it on a trusted LAN. If the host is exposed to untrusted networks,
restrict `8899` with your firewall, or bind it to a specific interface by
setting the port to `"<lan-ip>:8899:8899"` in `docker-compose.yml`. kokoro
(`8880`) and supertonic (`7788`) are loopback-only, so your CPU isn't
exposed for synthesis.

The mounted Pulse cookie and socket give the container full access to your
audio session (including capture, in principle). The mounts are read-only
and the server only ever spawns `paplay`, but treat the container as trusted.

## License

MIT — see [LICENSE](LICENSE).
