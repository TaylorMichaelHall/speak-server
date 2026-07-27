#!/usr/bin/env bash
# Speak text aloud via speak-server, which synthesizes through a TTS engine
# (kokoro or supertonic) and plays it on that machine's speakers.
#
# Usage: speak.sh "text to say"   |   echo "text" | speak.sh
#
# Env — all optional. Anything unset is simply not sent, so the server's own
# defaults apply and this client keeps working whatever that server is
# configured for:
#   SPEAK_HOST   server address (default 127.0.0.1:8899)
#   SPEAK_TOKEN  bearer token, if the server requires one
#   ENGINE       kokoro | supertonic | fastest | random
#   VOICE        engine-specific voice name
#   SPEED        playback speed (default 1.0)
#   LANG_CODE    supertonic only; 'ko', 'ja', … (default auto-detect)
#   PRIORITY     emergency | high | normal | low
#   SINK         output device, or a name from the server's AUDIO_ROUTES
#   VOLUME       0-100
#   NOWAIT       set to 1 to return as soon as it's queued, without waiting
#                for the audio to finish
#
# Exit status: 0 spoken (or queued, with NOWAIT=1), 1 anything else.
# Needs curl and jq.
set -euo pipefail

HOST="${SPEAK_HOST:-127.0.0.1:8899}"
SPEED="${SPEED:-1.0}"

TEXT="$*"
if [ -z "$TEXT" ]; then TEXT="$(cat)"; fi
if [ -z "$TEXT" ]; then echo "speak: no text given" >&2; exit 2; fi

RESP="$(mktemp)"
trap 'rm -f "$RESP"' EXIT

# Optional fields are added only when set. jq builds the JSON so text containing
# quotes, newlines or backslashes can't break the request.
BODY="$(jq -n \
  --arg t "$TEXT" \
  --arg e "${ENGINE:-}" --arg v "${VOICE:-}" \
  --arg l "${LANG_CODE:-}" --arg p "${PRIORITY:-}" --arg k "${SINK:-}" \
  --arg vol "${VOLUME:-}" --arg nowait "${NOWAIT:-}" \
  --argjson s "$SPEED" \
  '{text:$t, speed:$s}
   + (if $e     != "" then {engine:$e}          else {} end)
   + (if $v     != "" then {voice:$v}           else {} end)
   + (if $l     != "" then {lang:$l}            else {} end)
   + (if $p     != "" then {priority:$p}        else {} end)
   + (if $k     != "" then {sink:$k}            else {} end)
   + (if $vol   != "" then {volume:($vol|tonumber)} else {} end)
   + (if $nowait != "" then {wait:false}        else {} end)')"

AUTH=()
if [ -n "${SPEAK_TOKEN:-}" ]; then AUTH=(-H "Authorization: Bearer ${SPEAK_TOKEN}"); fi

# The server holds a synchronous request until playback finishes, so the timeout
# has to cover queueing + synthesis + the length of the audio. curl's -w prints
# the code (000 on connection failure); on curl error, reset to a clean 000.
HTTP="$(curl -s -m 300 -o "$RESP" -w '%{http_code}' \
  "http://${HOST}/speak" \
  -H 'Content-Type: application/json' "${AUTH[@]}" -d "$BODY" 2>/dev/null)" || HTTP=000

case "$HTTP" in
  000)
    # Couldn't reach the server. Distinguish "container not running" from "up but
    # not responding" so the terminal message is precise. Never try to start it.
    if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'speak-server'; then
      echo "speak: speak-server container is running but not responding at ${HOST}." >&2
    else
      echo "speak: speak-server docker container is not running — nothing to speak through. Start it yourself when you want TTS back." >&2
    fi
    exit 1 ;;
  200) exit 0 ;;
  202)
    # Queued but not yet spoken: NOWAIT, or the server deferred it to the end of
    # quiet hours. Either way it was accepted, so this is a success.
    head -c 300 "$RESP" >&2; echo >&2
    exit 0 ;;
  401)
    echo "speak: ${HOST} requires a token — set SPEAK_TOKEN." >&2
    exit 1 ;;
  409)
    # Refused on purpose: quiet hours, muted, or cut off by something urgent.
    echo -n "speak: not spoken — " >&2; head -c 300 "$RESP" >&2; echo >&2
    exit 1 ;;
  429)
    echo -n "speak: rate limited — " >&2; head -c 300 "$RESP" >&2; echo >&2
    exit 1 ;;
  *)
    echo "speak: speak-server returned HTTP ${HTTP}." >&2
    head -c 300 "$RESP" >&2; echo >&2
    exit 1 ;;
esac
