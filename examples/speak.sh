#!/usr/bin/env bash
# Speak text aloud via the local speak-server (docker, :8899), which synthesizes
# through a TTS engine (kokoro or supertonic) and plays on this machine's speakers.
# Usage: speak.sh "text to say"   |   echo "text" | speak.sh
# Env: ENGINE (kokoro|supertonic, default kokoro), VOICE (default af_heart /
#      M1 per engine), SPEAK_HOST (default 127.0.0.1:8899), SPEED (default 1.0)
set -euo pipefail

ENGINE="${ENGINE:-kokoro}"
# Voice names are engine-specific, so the fallback has to follow the engine.
case "$ENGINE" in
  supertonic) VOICE="${VOICE:-M1}" ;;
  *)          VOICE="${VOICE:-af_heart}" ;;
esac
HOST="${SPEAK_HOST:-127.0.0.1:8899}"
SPEED="${SPEED:-1.0}"

TEXT="$*"
if [ -z "$TEXT" ]; then TEXT="$(cat)"; fi
if [ -z "$TEXT" ]; then echo "speak: no text given" >&2; exit 2; fi

RESP="$(mktemp)"
trap 'rm -f "$RESP"' EXIT

BODY="$(jq -n --arg t "$TEXT" --arg e "$ENGINE" --arg v "$VOICE" --argjson s "$SPEED" \
  '{text:$t, engine:$e, voice:$v, speed:$s}')"

# The server holds the request until playback finishes, so the timeout covers
# synthesis + audio duration. curl's -w prints the code (000 on connection
# failure); on curl error, reset to a clean 000.
HTTP="$(curl -s -m 300 -o "$RESP" -w '%{http_code}' \
  "http://${HOST}/speak" \
  -H 'Content-Type: application/json' -d "$BODY" 2>/dev/null)" || HTTP=000

if [ "$HTTP" = "000" ]; then
  # Couldn't reach the server. Distinguish "container not running" from "up but
  # not responding" so the terminal message is precise. Never try to start it.
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'speak-server'; then
    echo "speak: speak-server container is running but not responding at ${HOST}." >&2
  else
    echo "speak: speak-server docker container is not running — nothing to speak through. Start it yourself when you want TTS back." >&2
  fi
  exit 1
fi
if [ "$HTTP" != "200" ]; then
  echo "speak: speak-server returned HTTP ${HTTP}." >&2
  head -c 300 "$RESP" >&2; echo >&2
  exit 1
fi
