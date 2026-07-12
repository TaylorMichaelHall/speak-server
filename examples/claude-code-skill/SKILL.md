---
name: speak
description: Speak text aloud on the user's machine via the local speak-server (docker, port 8899, fronts the Kokoro TTS container). Use to read a message, summary, or answer out loud. Triggers on "speak", "say that out loud", "read it to me", "tts this", "voice this".
argument-hint: "[text to speak] — or omit to speak a short summary of what just happened"
---

Speak text aloud through the local speak-server and the machine's speakers.

The server is a docker container (`speak-server`, in the kokoro-tts compose stack)
listening on port 8899. It synthesizes via the Kokoro container and plays the audio
itself — playback happens server-side, so the caller just POSTs text.

Pick the text:
- If the user passed text after `/speak`, speak that verbatim.
- If nothing was passed, speak a one-line summary of what just finished.
- Keep it to a sentence or two unless the user asked for more — CPU synthesis of long
  text takes a while and the request blocks until playback finishes.

Make the text TTS-friendly first. Kokoro reads raw identifiers, IDs, and symbols
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

Overrides via env vars:
- `VOICE=af_bella ...` — default is `af_heart`. List voices:
  `curl -s http://127.0.0.1:8880/v1/audio/voices | jq -r '.voices[].id'`.
- `SPEED=1.2 ...` — playback speed (default 1.0).
- `SPEAK_HOST=... ` — server address (default `127.0.0.1:8899`).

Gotchas:
- **Use `127.0.0.1`, never `localhost`** for anything in this stack — containers bind
  IPv4; `localhost` can resolve to IPv6 `::1` and curl fails. The wrapper already uses
  `127.0.0.1`.
- The request is synchronous — it blocks until the audio finishes playing. For long
  text, run it in the background if you don't want to wait.
- If the wrapper reports the container isn't running, **just relay that to the user in
  the terminal — do not try to start the container yourself.** Starting it is their call.
- A 500 from the server means synthesis worked but playback failed (e.g. no desktop
  audio session). Surface it; don't claim it spoke.

A successful run prints nothing and exits 0. If it errors, surface the message plainly
rather than claiming it spoke — the desktop audio session must be active.
