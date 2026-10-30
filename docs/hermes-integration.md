# Running the trading bot under Hermes Agent (local Qwen3.8-27B via llama.cpp)

How [Nous Research Hermes Agent](https://hermes-agent.nousresearch.com/) manages this
repo, driven by the model served locally by
[llama.cpp](https://github.com/ggml-org/llama.cpp) (`Qwen3.8-27B`, Q4_K_M GGUF).

Two distinct things have to line up, and it is easy to conflate them:

1. **Hermes as a model client** — Hermes needs an LLM. That LLM is `llama-server`'s
   local endpoint (`http://127.0.0.1:1919/v1`), which is OpenAI-compatible
   (`/v1/chat/completions`, `/v1/models`). It does **not** serve the Anthropic API, so
   Hermes must run in `chat_completions` mode. There is no auto-wiring CLI: the provider
   block is written by hand (Step 2).
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
| **llama.cpp (`llama-server`)** | Plain C++ GGUF runtime. Splits the model across GPU and CPU, serves the OpenAI API, and parses tool calls through the model's own Jinja chat template. No Python, no venv, no kernel-cache wheels — one binary plus its CUDA libs. |
| **This repo** | Already speaks MCP. `mcp_server/` is a stdio `FastMCP` server (~52 tools) that proxies to the running web UI, with destructive ops default-deny. |

The repo's README already documents wiring an MCP client to `run_mcp.sh`
(`README.md` § *Operate via MCP*). Hermes is just another MCP client with an extra
model-provider step.

---

## Architecture

```
                   ┌──────────────────────────┐
   Qwen3.8-27B     │  llama-server            │  OpenAI compatible
   (Q4_K_M, 17.4GB)│  127.0.0.1:1919          │  /v1/chat/completions, /v1/models
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

- **llama-server must be serving the model** — `/v1/models` reachable at
  `127.0.0.1:1919`.
- **The web UI must be running** (`python main.py --web` / `./run_web.sh --no-tunnel`) —
  the MCP tools proxy to its `/api/*`; it is the sole owner of paper books and backtest
  jobs. `server_health` / `curl 127.0.0.1:8080/health` verifies it.

---

## Prerequisites

| Need | Check |
|---|---|
| Hermes installed | `hermes doctor`; install: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` |
| llama.cpp binaries + a GGUF | `llama-server --version`; a prebuilt CUDA archive unpacked under `~/.local/opt/llama.cpp` with its bundled `cudart-*` dir is enough (no CUDA toolkit needed) |
| A GPU that can hold most of the model | `nvidia-smi -L` — a 16 GB card keeps ~50 of this model's 65 layers on the GPU |
| Repo venv + deps | `<repo>/.venv/bin/python -c "import mcp_server"` |
| Filled `.env` with `WEB_UI_PASSWORD` | `python main.py --web` starts cleanly |
| Web UI up | `curl -fsS http://127.0.0.1:8080/health` → `{"ok":true}` |

---

## Step 1 — Serve the model, then point Hermes at it

```bash
# 1. Prebuilt CUDA build: its own libs must be on the loader path.
. ~/.local/opt/llama.cpp/env.sh

# 2. Start the server (see the context/VRAM section below before changing -c)
llama-server -m ~/models/qwen3.8-27b/Qwen3.8-27B-Q4_K_M.gguf \
  -c 262144 -fa on -ctk q4_0 -ctv q4_0 -t 20 -b 2048 -ub 512 --parallel 1 \
  --jinja -a qwen3.8 --host 127.0.0.1 --port 1919
#    → wait for: "main: model loaded" / "srv  llama_server: listening on http://127.0.0.1:1919"

# 3. Grab the exact served model id (`-a`) and what the build reports
curl -s http://127.0.0.1:1919/v1/models          # → ids: ["qwen3.8"]
curl -s http://127.0.0.1:1919/props | jq '{model_alias, model_path, total_slots, n_ctx: .default_generation_settings.n_ctx, modalities, tools: .chat_template_caps.supports_tools}'
```

### ⚠ Context vs. VRAM (read this)

This model is **dense 27B** (17.4 GB at Q4_K_M) on a 16.3 GB card, and it is **hybrid**:
only 16 of its 65 layers are full attention (the rest are SSM/gated-delta-net). So the KV
cache is the context lever, and it is not cheap:

| context | q8_0 KV | q4_0 KV | note |
|---|---|---|---|
| 64K | 2.1 GiB | 1.1 GiB | fastest turns; `-ngl 50` measured 9.8 tok/s |
| 128K | 4.2 GiB | 2.2 GiB | measured 7.8 tok/s (auto split) |
| **256K** | 8.5 GiB | 4.5 GiB | **native max** (`n_ctx_train` 262144); measured 7.5 tok/s — what the unit uses |
| 1M | 34 GiB | 18 GiB | **not reachable**: KV alone exceeds the card, before weights |

- **1M context is a hardware limit, not a flag.** It would also need YaRN
  (`--rope-scaling yarn --yarn-orig-ctx 262144`), on top of 18 GiB of q4_0 KV. Putting KV
  in host RAM (`--no-kv-offload`) makes it *possible* on paper and unusable in practice.
- **Leave `-ngl` unset at 128K+** so `common_fit_params` sizes the GPU/CPU split to free
  memory; pinning `-ngl 50` at 256K OOMs. Pinning only wins at 64K (~2 tok/s over auto).
- Quant alternates sit next to the model (Q5_K_M 19.5 GB, Q8_0 29.0 GB). They buy fidelity
  and lose layers to the CPU: `~/.local/opt/llama.cpp/run-qwen3.8-27b.sh` documents the
  measured per-quant numbers (Q4_K_M is the fastest at 16 tok/s with an 8K context).
- `--jinja` is **mandatory** for tool calling: it makes the server render the model's
  Qwen chat template and parse `tool_calls` out of the output.
- Set Hermes's `model.context_length` to the same `-c` you serve:

```yaml
# ~/.hermes/config.yaml
model:
  provider: custom:llama
  default: Qwen3.8-27B-Q4_K_M
  base_url: http://127.0.0.1:1919/v1
  api_mode: chat_completions
  context_length: 262144
  supports_vision: false       # text-only GGUF, see "Vision" below
```

### Context compaction needs a budget — not a cooldown

On this window Hermes compacts at **85% (222,822 tokens at 256K) no matter what
`compression.threshold` says**: the small-context floor binds, and the trigger is capped at
85% of the usable window. Everything else follows from that number.

- Compaction is itself an LLM call over the transcript, so it costs a full long-context
  pass: prefill at ~800 tok/s plus ~4.5 tok/s decode at depth.
- **The summary request carries no output reservation.** Hermes reports
  `aux_output_reservation: null` and the server sees `max_tokens: -1`, so Qwen keeps
  generating (thinking included) until the ceiling. Measured on the failing attempt:
  a 6.8K-token prompt streamed for **1,201s** and was cut off uncommitted — 6.8K tokens of
  *input*, because Hermes bounds what it summarises. Worse, with `--parallel 1` that
  abandoned stream kept the **only slot** busy, which is why "the model may stop
  responding".
- The summary budget scales with the window: `min(ctx × 0.05, 10_000)`, so **10,000
  tokens at 256K**. A cap below that produces
  *"summary was truncated (finish_reason=length)"* and the attempt is discarded — the cap
  must therefore sit at or above the budget, not below it.
- Two separate ceilings apply, and both default too low: the host compaction ceiling
  (600s) and the auxiliary stream ceiling, which is `max(600, 4 × auxiliary.compression.timeout)`
  — the stock 300s timeout therefore yields **1200s** and reports
  *"Auxiliary streamed call timed out after 1200s total ceiling (stream still open but
  over budget)"*.
- The visible warning — *"Context is over the compression threshold … but compression is
  currently blocked (cooldown:300)"* — is a **downstream effect** of those timeouts, not
  the cause.
- The failure cooldown is persisted on the session row
  (`sessions.compression_failure_cooldown_until`), so restarting the service does not
  clear it; it expires on its own.

Size both budgets to the model, and bound the summary:

```yaml
# ~/.hermes/config.yaml
compression:
  context_timeout_seconds: 1500         # idle window, long enough for a 10K-token summary
  context_total_ceiling_seconds: 2400   # host backstop above it
  proactive_prune_tokens: 30000         # deterministic prune before the LLM is needed
  proactive_prune_min_result_chars: 8000
auxiliary:
  compression:
    timeout: 1500                       # -> aux stream ceiling becomes 6000s
    extra_body:
      max_tokens: 10000                 # == Hermes' summary budget at 256K
      chat_template_kwargs:
        enable_thinking: false          # Qwen thinking otherwise eats the whole budget
```

Verified: the server logs the summary request with the configured cap (previously `-1`),
`enable_thinking: false` yields zero reasoning tokens, and a summary that fits the cap
commits with `method: llm_summary`.

`proactive_prune_tokens` is the cheap lever: it drops/summarises old tool results with **no
model call**, and that is what actually keeps sessions away from the trigger (this repo's
MCP tools return large JSON). With it enabled, LLM compaction only fires in very long
sessions.

A healthy compaction writes one `[CONTEXT COMPACTION — REFERENCE ONLY]` message and marks
the prior turns `compacted=1` in `~/.hermes/state.db` (`messages`). A
"deterministic fallback summary" warning instead means the summary route still stalled —
raise the ceiling again or start a fresh session.

### Vision

`GET /props` reports `modalities` (`{"vision": true}` only when an `mmproj` encoder was
loaded with `--mmproj`). This GGUF is **text-only**, so `model.supports_vision` must be
`false`: if it is set to `true`, Hermes sends images natively, llama-server rejects them,
and the chart PNGs this repo renders (`analyze_symbol(save_chart=true)`,
`paper_trade_chart`, `get_pattern_run_chart`) cannot be read by the agent at all.

Chart review therefore needs either a multimodal GGUF (plus its `mmproj`) or an auxiliary
vision model routed through `auxiliary.*`, not this endpoint.

### Tool calling and reasoning output

Verify tools before trusting a multi-step loop:

```bash
curl -s http://127.0.0.1:1919/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "qwen3.8",
  "messages": [{"role":"user","content":"Weather in Tokyo? Use the tool."}],
  "tools": [{"type":"function","function":{"name":"get_weather",
             "parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]
}'
# → choices[0].finish_reason == "tool_calls", choices[0].message.tool_calls populated.
```

Raw JSON printed as `content` instead means `--jinja` is missing (or a custom
`--chat-template` broke it). `/props` → `chat_template_caps.supports_tools` should be
`true`.

Qwen's thinking lands in `reasoning_content`, separate from the visible `content`. With a
tiny `max_tokens` the whole budget can be spent on reasoning and `content` comes back
empty — a budget artifact, not a failure. llama-server applies no output cap of its own,
and Hermes does not cap output for custom endpoints.

---

## Step 2 — Point Hermes at the model

There is no `ft launch hermes` equivalent here: write the provider block yourself. The
local endpoint needs no API key, so no credential entry is involved.

```yaml
# ~/.hermes/config.yaml
model:
  provider: custom:llama
  default: Qwen3.8-27B-Q4_K_M
  base_url: http://127.0.0.1:1919/v1
  api_mode: chat_completions
  supports_vision: false
  context_length: 262144
custom_providers:
  - name: llama
    base_url: http://127.0.0.1:1919/v1
    model: Qwen3.8-27B-Q4_K_M
    api_mode: chat_completions
    models:
      Qwen3.8-27B-Q4_K_M:
        context_length: 262144
```

Confirm with `hermes status`: it prints the model and provider it resolved
(`Provider: custom:llama`).

> **Model name is a label, not a selector.** llama-server serves whatever GGUF it was
> started with; `-a` only sets the advertised id. Keep `model.default` aligned with the
> alias (or with `/v1/models`) so the two agree, but nothing is loaded on request.
>
> **Context length is a contract, not a wish.** Set `model.context_length` to the `-c` you
> actually serve. Hermes compresses history before that limit; if it is set higher than
> the server holds, long turns fail at runtime.

---

## Step 3 — Give Hermes the trading MCP server

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

## Step 4 (optional) — Let Hermes also work the *code*

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

## Step 5 — Make it survive reboots

Everything the agent needs should come back on its own after a restart. This host already
runs the bot's web UI as a **systemd user service** with `Restart=always`, and user-systemd
**linger** is enabled (`loginctl show-user $USER | grep Linger` → `yes`), so user services
start at boot without an interactive login.

Add a unit for the model server, modelled on the repo's web unit:

```ini
# ~/.config/systemd/user/llama-server.service
[Unit]
Description=llama.cpp server (Qwen3.8-27B Q4_K_M, OpenAI-compatible)
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=%h
# Prebuilt CUDA binaries + their bundled cudart (no CUDA toolkit on this host).
Environment=LD_LIBRARY_PATH=%h/.local/opt/llama.cpp/cudart-llama-b11514-bin-ubuntu-cuda-13.4-x64:%h/.local/opt/llama.cpp/llama-b11514
# -ngl is left unset so the build auto-fits the GPU/CPU split; 256K is the model's native
# max with q4_0 KV (4.5 GiB). Pinning -ngl at this context OOMs.
# --parallel 1 is load-bearing: the default 4 slots share ONE KV cache, so concurrent
# sessions starve each other ("failed to find free space in the KV cache").
ExecStart=%h/.local/opt/llama.cpp/llama-b11514/llama-server \
  -m %h/models/qwen3.8-27b/Qwen3.8-27B-Q4_K_M.gguf \
  -c 262144 -fa on -ctk q4_0 -ctv q4_0 -t 20 -b 2048 -ub 512 --parallel 1 \
  --jinja -a qwen3.8 --host 127.0.0.1 --port 1919
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
After=llama-server.service trading-web.service
Wants=llama-server.service trading-web.service
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now llama-server.service
systemctl --user list-dependencies default.target   # confirm all units are pulled in
systemctl --user is-enabled llama-server hermes-gateway trading-web
```

> **Port 1919 is exclusive.** Anything else started by hand (a shell-run `llama-server`,
> an old engine) makes the unit crash-loop on `EADDRINUSE`. Find the holder with
> `ss -tlnp | grep 1919`, stop it, and let systemd take the port.

---

## Operations

```bash
systemctl --user status  llama-server hermes-gateway trading-web   # states
systemctl --user restart llama-server.service                      # after changing flags
journalctl --user -u llama-server -f                               # model-server log
journalctl --user -u hermes-gateway -f                              # gateway log
curl -s http://127.0.0.1:1919/health                                # {"status":"ok"}
curl -s http://127.0.0.1:1919/props | jq '{model_alias, modalities, n_ctx: .default_generation_settings.n_ctx}'
nvidia-smi --query-gpu=memory.used --format=csv,noheader            # VRAM headroom
hermes status                                                       # model, gateway, jobs
hermes -z "<prompt>"                                                # one-shot, headless
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
| Hermes (the agent) | Qwen3.8-27B (Q4_K_M) via llama.cpp | `~/.hermes/config.yaml` |
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
| `model.provider: custom:llama` | Name of the self-hosted OpenAI-compatible provider below |
| `model.base_url` | `http://127.0.0.1:1919/v1` (llama-server) |
| `model.api_mode` | `chat_completions` — llama-server has no `/v1/messages` |
| `model.default` | the served alias (`-a`) from `/v1/models` |
| `model.context_length` | same value as `-c` (here `262144`) |
| `model.supports_vision` | `false` for a text-only GGUF; `true` only with `--mmproj` |
| `custom_providers[]` | `name: llama` + `models:` map carrying the context length |
| `mcp_servers.trading-bot.*` | stdio MCP server (`run_mcp.sh`) |
| `auxiliary.*` | route compression/vision elsewhere; the local 27B cannot see images |

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
| `CUDA error: out of memory` at startup | `-ngl` too high for free VRAM. Lower it by 2 and restart; check `nvidia-smi` for what else holds the card. |
| `failed to find free space in the KV cache`, requests failing at ~38K | Default slot count too high. Serve with `--parallel 1`: 4 slots share one unified 65536-token cache, so long sessions evict each other. |
| Compaction stalls / "blocked (cooldown:300)" | Ceilings and the summary cap were sized for a smaller window. See [Context compaction needs a budget](#context-compaction-needs-a-budget--not-a-cooldown). |
| "summary was truncated (finish_reason=length)" | `auxiliary.compression.extra_body.max_tokens` is below Hermes' budget of `min(ctx × 0.05, 10000)`; raise it to that. |
| Generation crawls at ~4 tok/s | Most layers are on the CPU. Raise `-ngl` until just under the OOM point (see Step 1's table). |
| Model prints tool calls as text / "no tool support" | `--jinja` missing (or a custom `--chat-template` broke parsing). Confirm `/props` → `chat_template_caps.supports_tools`. |
| Hermes context-limit refusal at startup | Served window < 64K — raise `-c` and pin `model.context_length` to match. |
| `EADDRINUSE` on 1919, service crash-loops | Another `llama-server` (started by hand) owns the port: `ss -tlnp \| grep 1919`, stop it, restart the unit. |
| Empty assistant `content` | Tiny `max_tokens` consumed by reasoning; the answer is in `reasoning_content`. Give it output budget. |
| Hermes won't see chart images | Expected — this is a text-only GGUF. Keep `model.supports_vision: false` and use an auxiliary vision model. |
| `mcp_trading-bot_*` tools missing | `hermes mcp test trading-bot`; confirm `run_mcp.sh` is executable and absolute; check no `tools.include` filtered them; `/reload-mcp`. |
| "web service unreachable" | `python main.py --web` isn't running, or `MCP_API_URL` is wrong — check `/health`. |
| HTTP 401 from a tool | Bad `MCP_*` / `WEB_UI_*` credentials. |
| `No module named mcp_server` | Launch via `run_mcp.sh` (it `cd`s to the repo root). |
| Destructive/Maintenance/Deploy tool "disabled" | Set the matching `MCP_ALLOW_*` in `.env` and restart the Hermes session. |

---

## Sources

- [Hermes Agent — installation](https://hermes-agent.nousresearch.com/docs/getting-started/installation)
- [Hermes Agent — LLM and model providers](https://hermes-agent.nousresearch.com/docs/integrations/providers)
- [Hermes Agent — MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)
- [Hermes Agent — Use MCP with Hermes](https://hermes-agent.nousresearch.com/docs/guides/use-mcp-with-hermes/)
- [llama.cpp — server docs](https://github.com/ggml-org/llama.cpp/tree/master/tools/server) and [releases](https://github.com/ggml-org/llama.cpp/releases)
