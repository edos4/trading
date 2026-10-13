# Running the trading bot under Hermes Agent (local Qwen3.6-35B via FreeToken)

How [Nous Research Hermes Agent](https://hermes-agent.nousresearch.com/) manages this
repo, driven by the model served locally by
[FreeToken](https://github.com/FlashML-org/FreeToken) (`Qwen/Qwen3.6-35B-A3B-FP8`).

Two distinct things have to line up, and it is easy to conflate them:

1. **Hermes as a model client** — Hermes needs an LLM. That LLM is FreeToken's local
   endpoint (`http://127.0.0.1:1919/v1`), which serves both the OpenAI and Anthropic
   APIs. FreeToken also ships `ft launch hermes`, which writes Hermes's provider config.
2. **Hermes as an operator of the bot** — Hermes drives the bot through the MCP server
   this repo already ships (`mcp_server/`, stdio), plus its own shell/file tools.

Neither replaces the app's internal AI (Claude vision, DeepSeek pattern editor) — see
[Relationship to the in-app models](#relationship-to-the-in-app-models). Below, `<repo>`
is the checkout directory; host-specific paths, unit names and tunnels live in the
machine's own config, not here.

---

## Why it fits

| Piece | What it gives us |
|---|---|
| **Hermes Agent** | Self-hosted CLI/desktop agent (MIT). Persistent memory, auto-skills, scheduling, subagents, terminal + file tools, MCP **client** (stdio + HTTP), messaging gateway. |
| **FreeToken** | Edge MoE serving engine. Serves the OpenAI API (`/v1/chat/completions`, `/v1/responses`, `/v1/models`) **and** the Anthropic API (`/v1/messages`). |
| **This repo** | Already speaks MCP. `mcp_server/` is a stdio `FastMCP` server (~52 tools) that proxies to the running web UI, with destructive ops default-deny. |

The repo's README already documents wiring an MCP client to `run_mcp.sh`
(`README.md` § *Operate via MCP*). Hermes is just another MCP client with an extra
model-provider step.

---

## Architecture

```
                   ┌──────────────────────────┐
   Qwen3.6-35B-A3B │  FreeToken `ft serve`    │  OpenAI + Anthropic compatible
   (FP8, local GPU) │  127.0.0.1:1919          │  /v1/chat/completions, /v1/models
                   └────────────┬─────────────┘
                                │  model.base_url (OpenAI wire)
                   ┌────────────▼─────────────┐
                   │      Hermes Agent        │  memory · skills · scheduler
                   │  (~/.hermes/config.yaml) │  terminal · files · subagents
                   └───┬──────────────────┬───┘
        stdio MCP      │                  │  shell / filesystem / git
   (spawns run_mcp.sh) │                  │  (built-in tools + optional MCP servers)
                   ┌───▼──────────────┐   ┌▼─────────────────────────┐
                   │  mcp_server/     │   │  python main.py --web    │
                   │  (stdio, ~52     │──▶│  (state owner: paper     │
                   │   tools)         │HTTP│  books, backtest jobs)  │
                   └──────────────────┘ +Basic└──────────────────────────┘
```

Two hard prerequisites:

- **FreeToken must be serving the model** — `/v1/models` reachable at `127.0.0.1:1919`.
- **The web UI must be running** (`python main.py --web` / `./run_web.sh --no-tunnel`) —
  the MCP tools proxy to its `/api/*`; it is the sole owner of paper books and backtest
  jobs. `server_health` / `curl 127.0.0.1:8080/health` verifies it.

---

## Prerequisites

| Need | Check |
|---|---|
| Hermes installed | `hermes doctor`; install: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` |
| FreeToken installed | `ft --version` |
| A GPU that can hold the model | `nvidia-smi -L` |
| Repo venv + deps | `<repo>/.venv/bin/python -c "import mcp_server"` |
| Filled `.env` with `WEB_UI_PASSWORD` | `python main.py --web` starts cleanly |
| Web UI up | `curl -fsS http://127.0.0.1:8080/health` → `{"ok":true}` |

---

## Step 1 — Serve the model, then point Hermes at it

```bash
# 1. Start the engine (dtype/parsers/backends resolve from the checkpoint + GPU)
ft serve --model Qwen/Qwen3.6-35B-A3B-FP8
#    → wait for: "API server is ready to serve on 127.0.0.1:1919"

# 2. Grab the exact served model id (defaults to the checkpoint basename)
curl -s http://127.0.0.1:1919/v1/models

# 3. Configure + launch Hermes against it
ft launch hermes            # add --dry-run to preview, -y to auto-approve
```

`ft launch` discovers the model via `/v1/models`, writes Hermes's provider config,
installs the Hermes CLI if missing, and clears cloud keys from the child env so Hermes
cannot silently fall back to a paid endpoint.

### ⚠ The KV-cache gotcha (read this)

On a 16 GB card the engine's **auto** sizing favors a large MoE expert cache and hands the
KV pool only ~8,000 tokens — far below Hermes's **64K agent minimum**. Hermes will happily
start (it reads the model's advertised 256K window) and then fail at runtime once context
grows past the real KV pool.

Fix it explicitly. Force a split that fits the card's cache budget (~7.8 GiB here):

```bash
ft serve --model Qwen/Qwen3.6-35B-A3B-FP8 \
  --num-tokens 131072 \
  --moe-cache-size 1200
```

- `--num-tokens N` pins the KV pool. The model supports 256K, but KV is ~20 KB/token and
  the GPU also holds weights + MoE cache + Mamba state.
- **Setting `--num-tokens` alone OOMs**: it pins KV *without* shrinking the expert cache.
  Always pair it with a smaller `--moe-cache-size`.
- KV is cheap relative to experts, so trading expert slots for context is worthwhile
  (e.g. 128K KV + 1200 slots ≈ 7.4 GiB vs auto's 8K KV + 2607 slots ≈ 7.8 GiB).
- You can also resize a *running* server budget-aware without a restart:
  `ft ctl cache --kv 131072 --moe 1200` (returns HTTP 503 and keeps the old cache if it
  does not fit). Use this to find a working split, then bake it into the startup flags.
- Set Hermes's `model.context_length` to the same number so it compresses history before
  the server's real limit:

```yaml
# ~/.hermes/config.yaml
model:
  provider: custom
  default: Qwen3.6-35B-A3B-FP8
  base_url: http://127.0.0.1:1919/v1
  context_length: 131072
  supports_vision: true        # see "Vision" below
```

### Vision

`GET /v1/stats` reports `model.input_modalities` (`["text","image"]` when a vision encoder
is registered). Hermes does not know this model from models.dev, so set
`model.supports_vision: true` — otherwise Hermes pre-describes images instead of sending
them natively, and the chart PNGs (`analyze_symbol(save_chart=true)`,
`paper_trade_chart`, `get_pattern_run_chart`) go unread. Verify with a base64 image in an
OpenAI `image_url` block.

### Reasoning output

FreeToken serves Qwen with a reasoning parser (`/v1/stats` → `reasoning.gears`). Thinking
lands in `reasoning_content`, separate from `content`. With a tiny `max_tokens` the whole
budget can be consumed by reasoning and `content` comes back empty — a budget artifact,
not a failure. FreeToken's default output budget (32,768) is ample, and Hermes does not cap
output for custom endpoints.

---

## Step 2 — Give Hermes the trading MCP server

`mcp_server/` is **stdio**, so Hermes spawns it as a child process — nothing to firewall,
secrets stay in `.env`. Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  trading-bot:
    command: "<repo>/run_mcp.sh"     # cds to the repo root first
    args: []
```

Or via the CLI (discovery-first; connects, lists tools, writes the same block):

```bash
hermes mcp add trading-bot --command <repo>/run_mcp.sh
hermes mcp test  trading-bot        # connects, lists tools, non-zero exit on failure
hermes mcp configure trading-bot    # re-open the tool checklist later
```

Reload without a restart from inside a session with `/reload-mcp`. Tools appear to the
model as `mcp_trading-bot_<tool>`.

**Why `run_mcp.sh` and not `.venv/bin/python -m mcp_server`:** the launcher `cd`s to the
repo root first, so `.env` and repo-relative paths resolve regardless of the caller's cwd
(`mcp_server/config.py` reads `config.settings`, i.e. `.env`).

### What Hermes can then do

- **Inspect** — markets, symbol universes, history bars, `analyze_symbol` (volume gate,
  optional Kronos gate, saved chart PNG path).
- **Paper trade** — start/stop the US and PH books, positions with R-multiples, export
  trades, trade charts.
- **Backtest** — submit/cancel/retry runs, catalog + presets, quick status.
- **Patterns** — list registry/versions/diffs, trigger AI pattern edits.

### What is default-deny

`paper_reset`, `paper_reset_logs`, pattern `set_default`/`archive`,
`replay_clear`/`upload`, `db_check`/`db_update`, and `deploy*` each raise
`PermissionError` until the matching `MCP_ALLOW_*` flag is set in the repo's `.env`
(read at process start — restart the Hermes session after editing). There is **no live
order path** — `broker/` and `risk/` are empty.

To shrink the surface further, add a `tools.include` allowlist under
`mcp_servers.trading-bot` (Hermes accepts exact names and globs).

---

## Step 3 (optional) — Let Hermes also work the *code*

The MCP server manages the *running bot*. To let Hermes manage the *repository* — read/edit
code, run tests — use its built-in terminal and file tools, and/or add scoped MCP servers:

```yaml
mcp_servers:
  repo-fs:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "<repo>"]
    tools: { include: ["read_file", "list_directory", "search_files"] }
  repo-git:
    command: "uvx"
    args: ["mcp-server-git", "--repository", "<repo>"]
```

Keep the filesystem server rooted to the repo, not `$HOME`. Prefer the MCP tools for
anything touching the running paper books (the web UI is the sole state owner). Follow the
repo's conventions: host-specific values belong in a gitignored `.env`, and changes go in
multiple focused commits.

---

## Step 4 — Make it survive reboots

Everything the agent needs should come back on its own after a restart. This host already
runs the bot's web UI as a **systemd user service** with `Restart=always`, and user-systemd
**linger** is enabled (`loginctl show-user $ME | grep Linger` → `yes`), so user services
start at boot without an interactive login.

Add a unit for the model server, modelled on the repo's web unit:

```ini
# ~/.config/systemd/user/freetoken.service
[Unit]
Description=FreeToken model server (Qwen3.6-35B-A3B-FP8)
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=%h
# --num-tokens + --moe-cache-size pin a KV pool big enough for the agent (see Step 1).
ExecStart=%h/.freetoken/venv/bin/ft serve --model %h/.freetoken/models/Qwen3.6-35B-A3B-FP8 \
  --host 127.0.0.1 --port 1919 --moe-backend auto --max-running-requests 4 \
  --memory-ratio 0.85 --num-tokens 131072 --moe-cache-size 1200
Restart=always
RestartSec=10
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

Install the Hermes gateway (its scheduler + messaging surfaces) as a user service too:

```bash
hermes gateway install      # writes ~/.config/systemd/user/hermes-gateway.service, enables it
```

Order the gateway after its dependencies with a drop-in so it does not start before the
model server and web UI are up:

```ini
# ~/.config/systemd/user/hermes-gateway.service.d/after-stack.conf
[Unit]
After=freetoken.service trading-web.service
Wants=freetoken.service trading-web.service
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now freetoken.service
systemctl --user list-dependencies default.target   # confirm all units are pulled in
systemctl --user is-enabled freetoken hermes-gateway trading-web
```

> **Desktop-app conflict.** FreeToken Desktop launches its own `ft serve` on port 1919. If
> the systemd unit owns the port, do not start the model from the Desktop app at the same
> time. Killing the desktop app alone is not enough — its worker children reparent to
> `systemd --user` and keep port **1920** (the torch rendezvous store) and the GPU, causing
> an `EADDRINUSE` crash loop. Reap them (`kill` the leftover `ft serve` / `spawn_main`
> pids) before starting the service.

---

## Operations

```bash
systemctl --user status  freetoken hermes-gateway trading-web   # states
systemctl --user restart freetoken.service                      # after changing flags
journalctl --user -u freetoken -f                               # model-server log
journalctl --user -u hermes-gateway -f                          # gateway log
curl -s http://127.0.0.1:1919/v1/cache/status                   # KV/expert geometry
curl -s http://127.0.0.1:1919/v1/stats                          # throughput, modalities
hermes status                                                   # model, gateway, jobs
hermes -z "<prompt>"                                            # one-shot, headless
```

A typical verification prompt: *"Use the trading-bot MCP tools: call `server_health`, then
`list_symbols` for the us market."*

A useful scheduled habit is the MCP server's `daily_review(market="us")` prompt
(summarizes the paper book and proposes one next action) — run it on a cron via the
gateway.

---

## Relationship to the in-app models

Hermes's model and the app's internal AI are **separate**:

| Caller | Model | Configured in |
|---|---|---|
| Hermes (the agent) | Qwen3.6-35B-A3B-FP8 via FreeToken | `~/.hermes/config.yaml` |
| Chart vision confirmation | Anthropic Claude | repo `.env` (`VISION_CONFIRMATION_ENABLED`, `ANTHROPIC_*`) |
| AI pattern editing | DeepSeek | repo `.env` (`DEEPSEEK_*`) |

So `submit_pattern_edit` still spends DeepSeek credits and vision confirmation still calls
Claude, regardless of Hermes running on a local model. If "everything local" is the goal,
those two in-app integrations are the remaining cloud dependencies (the pattern-editor
provider is a single adapter in `ai/providers/deepseek.py`; vision is
`analysis/vision_checker.py`).

---

## Config reference

**Hermes side** (`~/.hermes/config.yaml` + `~/.hermes/.env`):

| Key | Meaning |
|---|---|
| `model.provider: custom` | Self-hosted OpenAI-compatible endpoint |
| `model.base_url` | `http://127.0.0.1:1919/v1` (FreeToken) |
| `model.default` | served model id from `/v1/models` |
| `model.context_length` | pin to the KV pool you allocated (e.g. 131072; ≥ 65536) |
| `model.supports_vision` | `true` — FreeToken serves image input |
| `mcp_servers.trading-bot.*` | stdio MCP server (`run_mcp.sh`) |
| `auxiliary.*` | route compression/vision elsewhere if the local 35B is weak there |

**Repo side** (`.env`, unchanged — the MCP server already reads it):

| Variable | Default | Meaning |
|---|---|---|
| `MCP_API_URL` | `http://127.0.0.1:8080` | Web UI the tools proxy to |
| `MCP_USERNAME` / `MCP_PASSWORD` | falls back to `WEB_UI_*` | Basic auth for `/api/*` |
| `MCP_TIMEOUT_SECONDS` | `60` | HTTP timeout for proxied calls |
| `MCP_MAX_HISTORY_BARS` | `2000` | Cap for `get_history_bars` output |
| `MCP_ALLOW_DESTRUCTIVE` | `false` | reset/reset-logs, set-default/archive, replay clear/upload |
| `MCP_ALLOW_MAINTENANCE` | `false` | `db_check` / `db_update` |
| `MCP_ALLOW_DEPLOY` | `false` | `deploy_status` / `deploy` |
| `MCP_DEPLOY_URL` / `MCP_DEPLOY_TOKEN` | `http://127.0.0.1:8099` / empty | Deploy gateway |

---

## Safety model

- **No live trading.** Broker/risk unimplemented; paper and backtest only.
- **Default-deny mutations.** Every destructive tool is gated by an `MCP_ALLOW_*` flag.
- **Allowlist the MCP surface** with `tools.include` for a financial system; add tools
  deliberately.
- **One state owner.** The MCP server never starts a second engine, so it cannot corrupt a
  running book's ledger.
- **Unattended runs need care.** Hermes can require approval (CLI prompt / Telegram / Slack
  buttons) for tool calls. Decide which actions auto-proceed before letting it run headless
  (`--yolo` disables approval — avoid it for anything that mutates state).
- **Secrets** stay in the repo `.env`; the MCP server opens no port.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `EADDRINUSE` on port **1920**, service crash-loops | Orphaned FreeToken children (from a killed Desktop app) hold the torch rendezvous port/GPU. Reap the leftover `ft serve` / `spawn_main` pids, then restart. |
| CUDA OOM on startup after adding `--num-tokens` | KV pinned without shrinking the expert cache. Add `--moe-cache-size` (see Step 1). |
| `HTTP 503` from `ft ctl cache` | Requested cache exceeds the budget; lower `--kv`/`--moe`. The old cache is kept. |
| Hermes context-limit refusal at startup | Local window < 64K — allocate ≥ 64K KV and pin `model.context_length` ≥ 65536. |
| Model prints tool calls as text | Tool-call parser off — FreeToken auto-infers it (`qwen3_coder` here); set `--tool-call-parser` explicitly and restart. |
| `mcp_trading-bot_*` tools missing | `hermes mcp test trading-bot`; confirm `run_mcp.sh` is executable and absolute; check no `tools.include` filtered them; `/reload-mcp`. |
| "web service unreachable" | `python main.py --web` isn't running, or `MCP_API_URL` is wrong — check `/health`. |
| HTTP 401 from a tool | Bad `MCP_*` / `WEB_UI_*` credentials. |
| `No module named mcp_server` | Launch via `run_mcp.sh` (it `cd`s to the repo root). |
| Empty assistant `content` | Tiny `max_tokens` consumed by reasoning; the answer is in `reasoning_content`. Give output budget (FreeToken default 32K). |
| Hermes won't see chart images | Set `model.supports_vision: true`. |
| Destructive/Maintenance/Deploy tool "disabled" | Set the matching `MCP_ALLOW_*` in `.env` and restart the Hermes session. |

---

## Sources

- [Hermes Agent — installation](https://hermes-agent.nousresearch.com/docs/getting-started/installation)
- [Hermes Agent — LLM and model providers](https://hermes-agent.nousresearch.com/docs/integrations/providers)
- [Hermes Agent — MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)
- [Hermes Agent — Use MCP with Hermes](https://hermes-agent.nousresearch.com/docs/guides/use-mcp-with-hermes/)
- [FreeToken — README](https://github.com/FlashML-org/FreeToken) and [CLI reference](https://github.com/FlashML-org/FreeToken/blob/main/docs/cli.md)
