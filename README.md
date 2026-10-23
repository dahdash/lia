# Lia — «لیایار», a Persian assistant for websites on Liara

**[فارسی](README.fa.md)** · English

Lia deploys, operates, monitors and troubleshoots websites on [Liara](https://liara.ir) for people who are not infrastructure experts. You talk to it in plain Persian ("my site is down", "deploy my shop", "connect my domain"). It calls Liara's real APIs, shows every step, explains the cause of a problem with evidence, and **asks before anything risky**. After a fix, it checks that the fix actually worked.

It comes as two front-ends over one tool layer:

- a **Persian chat web UI** with a live health sidebar and alerts;
- an **MCP server**, so Claude Desktop / Cursor / any MCP client can use the same tools with the same safety rules.

🎬 **Demo video (~5 min):** _link added after recording_

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[mcp,dev]'   # Python 3.11+
cp .env.example .env    # set LIARA_API_KEY (console.liara.ir → API), LIARA_TEAM_ID if needed, BOX_API_TOKEN
.venv/bin/lia           # web UI on http://127.0.0.1:8000
.venv/bin/lia-mcp       # MCP server over stdio
.venv/bin/pytest -q     # tests (offline, no Liara calls)
```

Lia can deploy local projects from `LIA_PROJECTS_DIR` (default `./demo`). The demo includes `demo/shop`, a working version, and `demo/shop-v2`, which has a deliberate bug: it crashes when `DISCOUNT_PERCENT` is missing, and `MEMORY_HOG_MB` forces it to run out of memory.

## The end-to-end scenario

> User: «سایت فروشگاهم خطا می‌ده، درستش کن» ("my shop site is throwing errors, fix it")

1. **Diagnose (automatic, read-only).** `diagnose_app` collects the app status, a probe of the site URL (HTTP 500), the error logs of the running release only, platform events (restarts, OOM kills), CPU/RAM against the plan limit, and the release history including what the last change was.
2. **Ranked root causes.** The answer is in Persian and lists the likely causes in order. Each cause has a confidence level and the evidence behind it, e.g. a quoted log line: `KeyError: 'DISCOUNT_PERCENT'` (high confidence: the last change was a deploy that reads a missing env var).
3. **Proposed fix and confirmation.** Lia proposes the cheapest fix first: set the variable, or roll back to the last good release. A paid option (a bigger plan) is offered as the alternative, with its price. Every action that changes something appears as a confirmation card. Nothing runs until the user clicks approve.
4. **Execute and verify.** Lia waits for the rollout and probes the site again until it responds. For memory problems, it re-reads the new container's RAM against the plan limit. It then says clearly whether the problem is solved. If it isn't, it moves on to the next cause.

Other things it handles:

- **first deploy:** it explains the ways to get a site onto Liara (Lia uploads the folder, the `liara` CLI, the console), detects the platform, port and suggested plan, and quotes the price before creating anything;
- **slow site / high resource use:** it weighs response time, RAM and CPU against the plan, OOM/restart events and app errors, and names the most likely cause;
- **improvement advice:** 2–3 concrete suggestions for speed and stability, backed by the data it read, saying which ones Lia can do and which belong in the code;
- restart/stop/start, env vars, plan changes, connecting a custom domain with DNS instructions (the user only does the steps Lia can't: DNS records, IRNIC), and "is my site OK?" questions.

## How Lia covers the challenge criteria

| Criterion | What Lia does |
|---|---|
| **Liara integration** (API, setup & deploy, service management, infra operations) | Real REST calls only (table below). Deploy = package → upload source → release → wait for the rollout → probe the site. Create app, change plan, restart/stop/start, env vars, rollback, domains. |
| **Intelligence** (log analysis, error detection, likely causes, fix, verifying the fix) | `diagnose_app` gathers logs of the running release, events, RAM/CPU vs. plan, the site probe and the last change; the answer ranks causes with confidence and quoted evidence, proposes the cheapest fix and, after it runs, re-probes the site (and RAM for memory issues) and says whether it worked. |
| **UX** (simple interaction, understandable suggestions, operation status, confirmation) | Plain Persian chat, raw status words translated, a live status line per tool call, confirmation cards with a Persian summary (and price + balance for costly actions), one-click "investigate" on alerts. |
| **Reliability & safety** (health/resource monitoring, automating repetitive work, safe sensitive actions, reporting API limits) | 60 s health watch with alerts for down/errors/RAM/CPU. Repetitive work is automated: the whole deploy pipeline, post-fix verification, env merging, keeping rollback sources, checking balance before costly calls. Risk tiers enforced in code, one-time MCP tokens, secret masking. `api_limitations` makes Lia say what the API can't do instead of faking it. |

## Architecture

```
            ┌──────────── Persian web UI (lia/web/static) ────────────┐
 user ─────▶│ chat · confirmation cards · live tool status · health   │
            └───────────────┬────────────────────────────┬────────────┘
                     /api/chat, /api/confirm       /api/status (sidebar)
                            │ NDJSON stream              │
                 ┌──────────▼──────────┐       ┌─────────▼──────────┐
                 │ Agent (lia/agent)   │       │ Health watch       │
                 │ LLM tool loop +     │       │ (lia/web/health)   │
                 │ confirmation gate   │       │ every 60 s, READ   │
                 └──────────┬──────────┘       └─────────┬──────────┘
 MCP client ─▶ lia-mcp ─────┤ (one-time confirm_token)   │
                 ┌──────────▼────────────────────────────▼──────────┐
                 │ Tool layer (lia/liara/tools.py)                   │
                 │ 24 tools, each with a risk tier: READ / SENSITIVE │
                 │ / COSTLY; diagnosis, log analysis, verification,  │
                 │ secret masking, input validation                  │
                 └──────────┬───────────────────────────────────────┘
                 ┌──────────▼──────────┐      ┌───────────────────────┐
                 │ Liara client        │─────▶│ api.liara.ir (REST)   │
                 │ (lia/liara/client)  │      └───────────────────────┘
                 └─────────────────────┘      .lia/ local deploy + env log
```

- **One tool layer, two front-ends.** `build_tools(settings)` builds the tool set once, and the web UI, the MCP server and the dev scripts all use it. A tool is a name, a description, a JSON schema, a **risk tier**, a handler and, for risky tools, a Persian confirmation summary.
- **Agent.** This is an OpenAI-compatible tool-calling loop. The provider is swappable; the default is boxapi with `gpt-5.4-mini`. Liara AI is not used, as the challenge rules require. Events are streamed to the UI as NDJSON, so the user sees each tool start and finish. The loop keeps the chat history valid even when a tool crashes or a turn is interrupted.
- **Health watch.** Every 60 s it lists the apps and, for each one, probes the site, reads metrics and scans the current release's logs for errors. It raises an alert when a problem starts (site down, errors, RAM ≥ 85 %, CPU ≥ 90 %) and an "ok" alert when it clears. Each alert has an "ask Lia about it" button. It only ever reads.
- **Diagnosis logic** (in the tools, not only the prompt):
  - logs are scoped to the running release;
  - deprecation warnings are dropped;
  - distinct exception lines are counted and ranked;
  - containers are ordered oldest-first, so the new container is measured after a rollout;
  - the last release (code deploy vs. env change) is linked to the OOM/error events of that release tag. This produces hints such as "rollback will NOT help, the env change caused it".

## How Lia connects to Liara's APIs

- It uses the REST API at `https://api.liara.ir` with `Authorization: Bearer <LIARA_API_KEY>`. When `LIARA_TEAM_ID` is set, every request also carries `?teamID=`, the same way the official CLI does it.
- The endpoints were verified against the official OpenAPI spec ([liara-cloud/openapi](https://github.com/liara-cloud/openapi)) and the official CLI source ([liara-cloud/cli](https://github.com/liara-cloud/cli)). The full map is in [docs/research/liara-api.md](docs/research/liara-api.md).

| Purpose | Endpoint | Tier |
|---|---|---|
| Apps, details, env vars | `GET /v1/projects`, `GET /v1/projects/{name}` | READ |
| Plans and price, balance | `GET /v1/me`, `GET /v1/billing` | READ |
| Runtime logs (per release) | `GET /v2/projects/{name}/logs?start=&releaseId=` | READ |
| Events (restart, OOM, deploy) | `GET /v1/projects/{name}/events` | READ |
| CPU / RAM | `GET /v1/projects/{name}/metrics/summary` | READ |
| Releases, state, build logs | `GET /v1/projects/{name}/releases`, `GET /v1/releases/{id}`, `GET /v2/releases/{id}/build-logs` | READ |
| Domains | `GET /v1/domains?project=` | READ |
| Deploy | `POST /v2/projects/{name}/sources` (tar.gz) → `POST /v2/projects/{name}/releases` | SENSITIVE |
| Restart / stop / start | `POST /v1/projects/{name}/actions/restart`, `…/actions/scale` | SENSITIVE |
| Env vars | `POST /v1/projects/update-envs` (Lia merges with the current list first) | SENSITIVE |
| Connect / remove domain | `POST /v1/domains`, `DELETE /v1/domains/{id}` | SENSITIVE |
| Delete app | `DELETE /v1/projects/{name}` | SENSITIVE |
| Create app, change plan | `POST /v1/projects`, `POST /v1/projects/{name}/resize` | COSTLY |

The site probe goes to `https://{app}.liara.run` directly. Before any costly call, Lia checks the balance against `minCreditAmount` and states the price per hour and per month from the plan list.

## Safety

- **Risk tiers.** READ tools run automatically. SENSITIVE and COSTLY tools never run without explicit approval. Only the user can approve: in the web UI that is the `/api/confirm` call behind the confirmation card, and the model has no way to approve by itself. If the user declines, the rest of that turn is skipped too. Every result and every error is shown to the user.
- **MCP.** A risky tool first returns the summary and a one-time `confirm_token`. The token is bound to that exact tool and those exact arguments, and it expires after 5 minutes. The action runs only when the same tool is called again with the token.
- **Secrets.** Env values whose names look like secrets (`*KEY*`, `*PASS*`, `*TOKEN*`, `DATABASE_URL`…) are masked as `***` in status, diagnosis and the env-change log. `.env` and `.lia/` are gitignored. The deploy archive skips `.env`, `.git`, `.venv` and similar files.
- **Input validation.** App names, ids and domains coming from the model are validated before they are put into a URL. Deploy paths cannot leave the projects folder. The web UI listens only on 127.0.0.1 and rejects foreign `Host` headers, which blocks DNS rebinding.

## Limitations

Lia tells the user about these instead of faking them (the `api_limitations` tool):

- **No rollback endpoint.** Lia rolls back by re-releasing a source it uploaded itself; it keeps the sourceIDs in `.lia/deploys.json`. Releases deployed by other tools (console, CLI, git) can't be rolled back through the API.
- **Env vars aren't part of a release,** so a rollback keeps a bad env value. Lia undoes env changes with `set_env`/`unset_env` instead.
- **No horizontal scaling,** only on (1) / off (0).
- **No HTTP request metrics or error rates.** 5xx errors are found from logs and by probing the site.
- **No alert, health-check or cron endpoints.** Lia's own health watch fills this gap, but only while Lia is running.
- **No server-side log search.** Logs are fetched by time window and filtered locally.
- **No shell/exec over REST.**
- **No domain purchase and no DNS editing.** Lia connects a domain the user already owns and tells them which DNS records to set: a pointing record (CNAME, or ALIAS/www for a root domain), a TXT ownership record with its value, and a CNAME for SSL, set at the DNS provider; for `.ir`, the nameservers are changed at IRNIC. Liara's API reports whether each record is set and the TXT value, but not the pointing/SSL target values, so Lia sends the user to the domain page in the Liara console for those.
- **Lia doesn't create networks.** Liara requires a private network for new apps. If there is none, Lia says so.

Limitations of the solution itself:

- The LLM can still be wrong. The tools give it structured evidence, and every change goes through the user, but the diagnosis is a ranked guess, not a proof.
- MCP confirmation relies on the MCP host showing the summary to a human. A client that approves its own tokens bypasses the human step. The web UI does not have this problem.
- Sessions, alerts and the health watch live in memory, for a single user on localhost. There are no accounts and no persistence beyond `.lia/`.
- Lia deploys from a local project folder (`LIA_PROJECTS_DIR`); it does not pull from git or build Docker images itself (Liara builds them).
- Lia only handles apps (PaaS). Databases, disks, object storage and mail are out of scope, although the API supports them.
- `add_domain` follows the spec but was not run live during development, because no domain was available for testing.

## User interaction

- **Web UI (Persian, RTL).**
  - **Chat.** Every tool call is shown as a status line with a spinner and then ✓ or ✗.
  - **Confirmation cards.** These show the Persian summary of the action. For costly actions they include the price and the account balance.
  - **Sidebar.** It lists each app's health (site, RAM/CPU against the plan, errors) and the alert feed. Each alert has a one-click "investigate" button.
  - **Answers.** They are short and in plain Persian. Raw API words such as `ACTIVE` or `oom` are translated into everyday Persian.
- **MCP.** Add to your client config:

  ```json
  {"mcpServers": {"lia": {"command": "/path/to/lia/.venv/bin/lia-mcp"}}}
  ```

  It uses the same tools and the same `.env`. Risky tools return a summary and a `confirm_token`, and you approve the action in the client's chat.

## Repository layout

```
lia/config.py         settings from .env
lia/liara/client.py   Liara REST client (validation, team id, v2 logs)
lia/liara/tools.py    tools, risk tiers, diagnosis, deploy/rollback, confirmations
lia/agent/loop.py     LLM tool loop, system prompt, confirmation flow
lia/web/              FastAPI server, health watch, Persian UI (static/index.html)
lia/mcp_server.py     MCP server with one-time confirm tokens
demo/                 demo sites (shop = healthy, shop-v2 = with a bug)
docs/research/        verified Liara API map
scripts/              dev helpers (live tool calls, chat client)
```
