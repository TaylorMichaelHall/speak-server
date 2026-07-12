# kokoro-tts + speak-server

Give the agents (or anything else) on your machine a voice. A two-container
compose stack:

- **kokoro** — [Kokoro FastAPI](https://github.com/remsky/Kokoro-FastAPI)
  (CPU image), an OpenAI-compatible TTS server on port **8880**.
- **speak-server** — a ~100-line Python HTTP server on port **8899**. POST it
  text; it synthesizes via Kokoro and plays the audio **on the host's
  speakers** through the mounted PulseAudio/PipeWire socket. The caller just
  POSTs text — no audio handling client-side.

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

# or JSON with voice/speed overrides
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"text": "Hello.", "voice": "af_bella", "speed": 1.2}' \
  http://127.0.0.1:8899/speak
```

- `GET /health` → `200 ok`
- `POST /speak` → `200 spoke` after playback finishes (the request **blocks
  until the audio is done playing**), `400` empty text, `502` Kokoro
  synthesis failed, `500` synthesis worked but playback failed (usually: no
  desktop audio session).
- List voices: `curl -s http://127.0.0.1:8880/v1/audio/voices`

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
reach it out of the box. kokoro (`8880`) stays on loopback — nothing external
needs it.

The container must run as the desktop user who owns the audio session. This
defaults to uid/gid 1000; if yours differ (`id -u`), export `UID` and `GID`
(or set them in a `.env` file next to the compose file) before `up`. The
Pulse cookie is mounted from `~/.config/pulse/cookie`.

Env knobs on speak-server: `VOICE` (default `af_heart`), `PORT`, `KOKORO_URL`,
`LEAD_SILENCE_MS` (default `500`) — silence prepended to each clip so the
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
(`8880`) is loopback-only, so your CPU isn't exposed for synthesis.

The mounted Pulse cookie and socket give the container full access to your
audio session (including capture, in principle). The mounts are read-only
and the server only ever spawns `paplay`, but treat the container as trusted.

## License

MIT — see [LICENSE](LICENSE).
