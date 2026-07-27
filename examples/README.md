# Examples

| | |
|---|---|
| `speak.sh` | Shell client. JSON escaping via `jq`, stdin support, precise errors. |
| `webhooks.json` | Annotated webhook receivers — copy to `data/webhooks.json`. |
| `homeassistant.yaml` | Home Assistant MQTT notify service and automations. |
| `claude-code-skill/` | A [Claude Code](https://claude.com/claude-code) `/speak` command. |

## From a shell

```sh
# the wrapper
examples/speak.sh "All tests passed."
some-command 2>&1 | tail -1 | examples/speak.sh
PRIORITY=high examples/speak.sh "Deploy to production failed."

# or plain curl, no dependencies
curl -sS -X POST --data "Backup finished." http://127.0.0.1:8899/speak
```

## Announce when a long command finishes

Sized for the case where you walk away from the terminal — `high` because you're
waiting on it, and the exit status decides what it says.

```sh
say-when-done() {
  "$@"
  local status=$?
  if [ $status -eq 0 ]; then
    PRIORITY=high NOWAIT=1 speak.sh "$1 finished."
  else
    PRIORITY=high NOWAIT=1 speak.sh "$1 failed with status $status."
  fi
  return $status
}

say-when-done make test
```

## From CI

Give the runner its own token so it can be revoked alone and shows up by name in
the history.

```yaml
# .github/workflows/build.yml
- name: Announce on the workstation
  if: always()
  run: |
    curl -sS -m 20 -X POST \
      -H "Authorization: Bearer ${{ secrets.SPEAK_TOKEN }}" \
      -H 'Content-Type: application/json' \
      -d '{"text": "Main build ${{ job.status }}.", "priority": "low", "wait": false}' \
      https://speak.internal.example/speak
```

`wait: false` matters here: a CI step shouldn't sit waiting for audio on someone
else's desk, and `low` means it expires unheard rather than queueing up if nobody
is around.

## From Python

No client library needed — it's one POST.

```python
import json
import urllib.request

def speak(text, priority="normal", host="127.0.0.1:8899", token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://{host}/speak",
        data=json.dumps({"text": text, "priority": priority, "wait": False}).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status

speak("The nightly job finished.", priority="low")
```

## Watch what it's doing

```sh
# one-line health
curl -sS 'http://127.0.0.1:8899/health?verbose=1'

# what's queued right now
curl -sS http://127.0.0.1:8899/api/queue | python3 -m json.tool

# the last ten things it said
curl -sS 'http://127.0.0.1:8899/api/history?limit=10' \
  | python3 -c 'import json,sys; [print(r["status"], "|", r["text"]) for r in json.load(sys.stdin)["rows"]]'

# follow every utterance over MQTT
mosquitto_sub -h 192.168.1.10 -t 'speak/event' -t 'speak/status'
```

Or just open `http://127.0.0.1:8899/`.
