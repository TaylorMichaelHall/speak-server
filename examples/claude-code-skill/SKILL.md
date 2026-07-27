---
name: speak
description: Speak text aloud on the user's machine via the local speak-server (docker, port 8899). Use to read a message, summary, or answer out loud. Triggers on "speak", "say that out loud", "read it to me", "tts this", "voice this".
argument-hint: "[text to speak] — or omit to speak a short summary of what just happened"
---

Speak text aloud through the local speak-server and the machine's speakers.

The server is a docker container (`speak-server`, in the speak-server compose stack)
listening on port 8899. It synthesizes via a configured TTS engine (kokoro by default,
or supertonic) and plays the audio itself — playback happens server-side,
so the caller just POSTs text.

Pick the text:
- If the user passed text after `/speak`, speak that verbatim.
- If nothing was passed, speak a one-line summary of what just finished.
- Keep it to a sentence or two unless the user asked for more — synthesis of long
  text takes a while and the request blocks until playback finishes.

Make the text TTS-friendly first. Every engine reads raw identifiers, IDs, and symbols
literally and wrong — pronunciation, not spelling, is what matters. Before speaking,
rewrite anything that won't say correctly. "Verbatim" above means the *content* is
verbatim; still transliterate hostile tokens so they sound right. This is a judgment
step, not a fixed find-replace — read the text and fix what would mangle:

- **Ticket / identifier codes** (`sc-1234`, `PR-42`): the hyphen reads as "minus" and
  the digits fuse into one big number. Spell the prefix, say the hyphen as "dash", and
  read digits individually → `sc-1234` becomes `s c dash one two three four`.
- **Long IDs / big integers** (`cust_id` 1639885, account numbers): read the
  digits one at a time rather than as a magnitude → `one six three nine eight eight
  five`, not "one million six hundred…". Short counts a human would actually say as a
  number ("3 files", "20 seconds") stay as-is.
- **snake_case / code identifiers**: drop or voice the underscore and expand if it helps.
  Watch initialisms that spell a word — `id` reads as the word "id", so write it `I D` to
  force the letters → `cust_id` → "cust I D", `org_id` → "org I D".
- **Symbols & structure**: URLs, file paths, `#4431`, version strings like `v3.1.1`,
  and stray punctuation all read badly. Say the human version ("PR forty four thirty
  one", "version three point one point one") or drop it — you're speaking, not dictating
  a document.

When in doubt, say it out loud in your head: if it wouldn't sound like how a person
would say it to a colleague, rewrite it.

Run the wrapper (handles JSON escaping, server-down detection, and error reporting):

```bash
~/.claude/skills/speak/speak.sh "The text to speak."
```

It also reads stdin, so `some-command | ~/.claude/skills/speak/speak.sh` works.

Overrides via env vars — all optional, and unset means "use whatever the server is
configured for", so the wrapper keeps working whatever engines that server has:

- `PRIORITY=emergency|high|normal|low` — where it sits in the queue, and whether it
  may cut off something already being spoken. Default `normal`. Use `high` for
  something the user is actively waiting on (a failure, a finished long build); use
  `low` for background chatter that can be interrupted or expire unheard. Reserve
  `emergency` for genuine alarms — it jumps the queue, cuts off whatever is talking,
  ignores quiet hours, and bypasses rate limits. Don't reach for it to be helpful.
- `NOWAIT=1` — return as soon as it's queued instead of blocking until the audio
  finishes. Use this for anything long, so the tool call doesn't sit there.
- `ENGINE=kokoro|supertonic|fastest|random` — `fastest` and `random` pick an
  engine per request, so leave `VOICE` unset with them and let each engine use
  its own default.
- `VOICE=af_bella ...` — voice names are engine-specific, so don't set one without the
  matching `ENGINE`: `af_heart` etc. for kokoro, `M1`–`M5`/`F1`–`F5` for supertonic.
- `SPEED=1.2` — playback speed (default 1.0).
- `SINK=desk` — a named output from the server's `AUDIO_ROUTES`, or a raw device name.
- `VOLUME=60` — 0-100.
- `SPEAK_HOST=...` — server address (default `127.0.0.1:8899`).
- `SPEAK_TOKEN=...` — only if that server requires one.

Gotchas:

- **Use `127.0.0.1`, never `localhost`** — the container binds IPv4; `localhost` can
  resolve to IPv6 `::1` and curl fails. The wrapper already uses `127.0.0.1`.
- The request is synchronous by default — it blocks until the audio finishes playing.
  For long text use `NOWAIT=1` rather than backgrounding the command.
- If the wrapper reports the container isn't running, **just relay that to the user in
  the terminal — do not try to start the container yourself.** Starting it is their call.
- **Exit 0 with output on stderr means it was accepted but not yet spoken** — either
  `NOWAIT=1`, or the server deferred it to the end of quiet hours. Say which, rather
  than claiming the user has heard it.
- **A 409 means the server deliberately didn't speak it**: quiet hours dropped it,
  playback is muted, or something more urgent cut it off. Relay the reason. Do not
  retry at a higher priority to force it through — the user configured that silence.
- **A 429 means rate limited.** Wait, don't retry in a loop.
- A 500 means synthesis worked but playback failed (e.g. no desktop audio session).
  Surface it; don't claim it spoke.
- A 502 means synthesis failed — the named engine is down, or its voice name
  didn't match. The body lists what each engine tried and said; relay that rather
  than retrying blind against a different engine.

A successful run prints nothing and exits 0. If it errors, surface the message plainly
rather than claiming it spoke — the desktop audio session must be active.

The user can see everything that was said, and replay it, at `http://127.0.0.1:8899/`.
Mention that if they ask what was spoken earlier; don't go reading the history
yourself unless they ask.
