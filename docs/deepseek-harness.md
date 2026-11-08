# DeepSeek Harness (`dsh`) — the /deepseek agent UI

Status: implemented and provisioned 2026-11-08. This document is the design
record and the runbook; behavior changes must update it.

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) is an
open-source agent harness: it reads and edits a workspace, runs commands, and
ships its own browser UI. The repo serves that UI same-origin under the existing
web login, so it is reachable at `https://<tunnel-host>/deepseek` as well as on
loopback.

**It has no navbar entry, by request: open `/deepseek` by URL.** The proxy does
not depend on that label, and the page is deliberately absent from the navbar so
it stays out of the operator's way — the Explorer/Backtest/Paper surface keeps
its usual shape:

```
Explorer | Backtest | Paper | Replay | Patterns | Indicators | Builder | Laya |
System | Settings | Hermes | Kronos | Documentation | Tasks
```

It is a **development tool, not a trading component**: nothing in the scanner,
paper trader, backtester or pattern editor calls it, and an unconfigured
`DEEPSEEK_HARNESS_URL` (the default) disables the mount.

## 1. What the harness is, and what it is not

| Fact | Consequence here |
| --- | --- |
| `dsh web` serves a local UI on a loopback port (3080) with **no auth of its own** — it trusts whoever can reach the socket. | Never exposed directly. `/deepseek` proxies it behind this app's login, and the proxy refuses anonymous callers. |
| It is a full agent: reads/writes files, runs commands in the workspace. | Loopback + login only. It starts in the repo root, so its default workspace is this checkout. |
| It ships its own session model: a per-process **launch token** exchanged for a signed, authority-bound cookie. | The launcher captures the token; the proxy replays it so a browser is signed in without anyone handling the credential. |
| The upstream project is in **developer preview** and iterates fast. | A daily timer checks npm and updates it; a breaking change shows up as an error on the page (503/timeout), never as a broken trading run. |

## 2. Provisioning (per host)

```bash
# 1. Install the CLI (node >= 20). The Hermes-bundled Node works and is what the
#    unit picks up when no system node is on PATH.
npm install --global --prefix ~/.local @deepseek-ai/dsh \
  --allow-scripts=@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs

# 2. Point the app at it (.env)
DEEPSEEK_HARNESS_URL=http://127.0.0.1:3080
DEEPSEEK_HARNESS_TOKEN_FILE=~/.local/state/deepseek-harness/token   # absolute path

# 3. Supervise it
systemctl --user enable --now deepseek-harness.service
```

`run_dsh.sh` is the supervisor: it launches `dsh web` on loopback with the
browser authority named as a trusted host, truncates the launch-token file, and
captures each new token from the startup line. It runs with the repo as the
working directory on purpose — the harness uses that directory as its default
workspace *and* reads its `.env` as a credential layer, so `DEEPSEEK_API_KEY`
(already present for pattern editing) is what the harness authenticates with.
No secret is copied anywhere else.

**Units** (host-local; kept out of `deploy/` deliberately, since this is a
developer tool rather than a trading service):

```ini
# ~/.config/systemd/user/deepseek-harness.service
[Unit]
Description=DeepSeek Harness (dsh) agent UI
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=%h/codes/trading
ExecStart=%h/codes/trading/run_dsh.sh
Restart=always
RestartSec=5
KillMode=control-group
TimeoutStopSec=20

[Install]
WantedBy=default.target
```

```ini
# ~/.config/systemd/user/deepseek-harness-update.{service,timer}
[Service]
Type=oneshot
ExecStart=%h/.local/bin/deepseek-harness-update
```

The timer runs daily (`RandomizedDelaySec=30m`, `Persistent=true`); the updater
installs a newer npm release only when one exists, then restarts the harness.
Logs: `journalctl --user -u deepseek-harness[-update]`.

## 3. The proxy contract (`web/deepseek_proxy.py`)

dsh publishes what a reverse proxy in front of it must do
(<https://deepseek-harness.github.io/deepseek-harness/en/guide/public-deployments>).
Each rule maps to code here, and each one was verified against the live
listener rather than assumed:

| Harness rule | Implementation |
| --- | --- |
| Preserve the browser-facing `Host` | Forwarded untouched on HTTP. On the WebSocket leg it travels as the *connect URI's* authority, because the client library appends an explicit `Host` rather than replacing its own, and a duplicate `Host` fails the fence with 403. |
| Strip the mount prefix | `/deepseek/api/x` → `/api/x`; the listener only serves origin-root routes. |
| Keep redirects inside the mount | Absolute `Location` values are re-prefixed; the launch exchange's `location: ./` already resolves against the mount and is left alone. |
| Rewrite cookie scope | The listener is plain loopback HTTP: its cookies are host-only `Path=/` with no `Secure`. They are narrowed to `Path=/deepseek/` and marked `Secure` when the browser leg is HTTPS, so the session cannot ride every request this origin serves. |
| Forward upgrades | `/api/remote.mux` (the session mux) is bridged to the listener; handshake headers are regenerated per connection. |
| Admit the authority | `run_dsh.sh` passes `--trusted-host`, and `--public-url` advertises the address browsers actually use. |

Two behaviours are this app's own, not the harness's:

- **Transparent sign-in.** A document request that comes back 401 (no session
  cookie: first visit, or the 30-day cookie expired) makes the proxy replay the
  launch token, so the harness itself answers the browser with `Set-Cookie` and
  the redirect. The printed tokenized URL never has to be opened by hand.
- **No cookie jar proxy-side.** The upstream client is a bare transport: it
  forwards the browser's `Cookie` and returns the harness's `Set-Cookie`
  untouched. An `httpx.AsyncClient` here silently swallowed the session cookie
  into its own jar and replayed it, which authenticated unrelated requests and
  could hand one browser another's session. Cookies belong to the browser.

Failure modes are honest: an unreachable listener answers 503 naming
`DEEPSEEK_HARNESS_URL`; an empty `DEEPSEEK_HARNESS_TOKEN_FILE` logs a warning and
leaves the 401 in place; a disabled setting falls through to the app, so
`/deepseek` 404s.

## 4. Verification (2026-11-08, live)

Run against the real harness and the real Cloudflare tunnel, not a double:
`web/test_deepseek_proxy.py` (19 tests) covers the prefix strip, redirect and
cookie rewrites, the launch-token replay, the anonymous and 503 paths, the WS
header contract, and the page's own visibility (served when configured, 404 when
not, and never linked from the navbar).

| Check | Result |
| --- | --- |
| Fresh document over the tunnel authority | 303 + `Set-Cookie … Path=/deepseek/`, then 200, `<title>DeepSeek Harness</title>` |
| Same on loopback (`localhost:8080`) | 303 → 200 |
| Session mux `wss://…/deepseek/api/remote.mux` | 101, frames accepted |
| API call with the wrong authority's cookie | 401 (fence passed, authority-bound cookie enforced) |
| Framing headers | no `X-Frame-Options` / `frame-ancestors` → the iframe renders |
| Real browser (`dsh` UI) | UI boots: workspace = the repo checkout, model button reads `DeepSeek V4 Flash` (the Command Code route — see §5) |
| Agent turn (`dsh headless`) | answers (first verified on the DeepSeek route, which resolved `DEEPSEEK_API_KEY` from the repo `.env`; now on the Command Code route, §5) |

## 5. Model provider: Command Code (default since 2026-11-08)

The harness runs on **Command Code** (`https://commandcode.ai`, the `commandcode`
CLI's own model gateway), not on the DeepSeek API. Everything below lives in the
harness's own `$DSH_HOME` — the trading repo's `.env` and code are untouched.

```
$DSH_HOME/cordis.patch.yml   # home-level patch: applies to *every* profile (web, headless, tui)
$DSH_HOME/.env               # COMMANDCODE_API_KEY=… (0600)
```

**Why pi-ai and not the built-in DeepSeek adapter.** The harness ships
`dsh-llm-deepseek-api-key`, whose transport speaks **Anthropic Messages**
(`baseURL` default `https://api.deepseek.com/anthropic`, wire path `/messages`).
Commandcode *does* expose a Messages route at `/provider/v1/messages`, but it
refuses the OSS/DeepSeek models:

```json
{"type":"error","error":{"type":"invalid_request_error","message":
 "Model \"deepseek/deepseek-v4-flash\" is not supported on this endpoint.
  Use /provider/v1/chat/completions for OpenAI and OSS models."}}
```

Every commandcode model advertises `supported_endpoints: ["/chat/completions",
"/responses"]`. So `DEEPSEEK_BASE_URL` is a dead end for these models, and the
route is declared through `dsh-llm-pi-ai` with `api: openai-completions`. The
built-in DeepSeek route stays mounted and still works against the real DeepSeek
API, so switching back is a one-line edit.

```yaml
# $DSH_HOME/cordis.patch.yml
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      commandcode:
        displayName: Command Code
        api: openai-completions
        baseURL: https://api.commandcode.ai/provider/v1
        apiKeyEnv: COMMANDCODE_API_KEY
        models:
          - id: deepseek/deepseek-v4-flash
            name: DeepSeek V4 Flash
            contextWindow: 1000000
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: commandcode
    model: deepseek/deepseek-v4-flash
```

Notes that save time:

- `apiKeyEnv` is a *credential reference*, resolved per request through the
  harness credential seam — inherited environment → `.credentials.yaml` → the
  invoking directory's `.env` → `$DSH_HOME/.env`. A reference that resolves to
  nothing fails the request with `MISSING_CREDENTIAL`. Keeping the key in
  `$DSH_HOME/.env` (not the repo `.env`) is deliberate: the agent's model bill
  and the app's trading config stay separate.
- Only the declared `models` are served; the Models page can *discover* the rest
  (`GET {baseURL}/models` with bearer auth — 96 models on this account) for
  adoption, but discovery stores nothing.
- Model ids are commandcode's own (`deepseek/deepseek-v4-flash`,
  `deepseek/deepseek-v4-pro`, `claude-sonnet-5-5`, `Qwen/Qwen3.8-Max`, …), not
  DeepSeek's `deepseek-flash` / `deepseek-v4-pro`.
- The key is the commandcode CLI's own credential
  (`~/.commandcode/auth.json` → `apiKey`, 93 chars) — the same key Hermes uses
  for provider `commandcode`.
- Other profiles inherit all of this: `cordis.patch.yml` at `$DSH_HOME` is the
  home-level layer, applied over every profile's own layer.

### Verify a provider change (the checks that actually discriminate)

```bash
export DSH_HOME=~/.local/share/deepseek-harness/home   # plus node on PATH
dsh web --dump-config | grep -A8 pi-ai        # the route is composed
dsh web --dump-config | grep -A4 agent-default-model

dsh headless "Reply with exactly: CC-OK"      # a real turn -> CC-OK
```

The discriminating test is the **negative control**: run the same turn with the
route pointed somewhere dead. It must fail, which proves the model traffic really
goes through this route rather than a leftover default.

```bash
cat > /tmp/cc_probe.yml <<'YAML'
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      commandcode: {api: openai-completions, baseURL: 'http://127.0.0.1:9/nowhere',
                    apiKeyEnv: COMMANDCODE_API_KEY, models: [{id: deepseek/deepseek-v4-flash}]}
YAML
dsh headless --patch /tmp/cc_probe.yml "Reply with exactly: SHOULD-NOT-HAPPEN"
# -> dsh: TRANSPORT: Connection error.   (exit 1)
```

Confirmed on 2026-11-08: normal run printed `CC-OK`; the dead-endpoint run failed
with `TRANSPORT: Connection error`; the live UI's model button reads
**DeepSeek V4 Flash** (this route's display name, where the DeepSeek API route
read `DeepSeek-V4.1-Flash`).

**Revert:** delete `$DSH_HOME/cordis.patch.yml` (or just the
`agent-default-model` entry to keep the route but not the default) and restart
`deepseek-harness.service`. The DeepSeek route is untouched throughout.

## 6. Operating notes

- **Model/workspace** are chosen in the harness UI itself (Settings → Models,
  and the workspace picker). The default is the Command Code route above; any
  OpenAI-compatible provider (including the llama.cpp endpoint on
  `127.0.0.1:1919`) can be added the same way.
- **Secrets**: two different keys, deliberately kept apart — the harness's model
  bill is `COMMANDCODE_API_KEY` in `$DSH_HOME/.env`; the repo's
  `DEEPSEEK_API_KEY` still belongs to the pattern editor and the (unused)
  built-in DeepSeek route. The harness also stores its cookie-signing secret in
  `$DSH_HOME/.credentials.yaml` (what keeps sessions valid across restarts).
- **Restarting the web app does not disturb the harness**, and vice versa.
  Restarting the harness invalidates launch tokens only — the proxy picks up the
  new token from the file.
- **Cost**: an agent turn spends DeepSeek API credit. The page is operator-driven;
  nothing schedules prompts.
