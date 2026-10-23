# Liara API — verified capabilities

Researched 2026-10-22. Sources, in order of authority:

1. **Official OpenAPI spec** — [liara-cloud/openapi](https://github.com/liara-cloud/openapi) `spec/*.yaml` (commit `338325a`, 2026-10-17). Rendered at openapi.liara.ir.
2. **Official CLI** — [liara-cloud/cli](https://github.com/liara-cloud/cli) v9.6.0 (commit `14ec35f`, 2026-10-18). Shows how endpoints are actually called today.
3. ~~razavioo/liara-mcp~~ (third-party) — **not trusted**: several paths contradict the official spec/CLI (e.g. `/v1/zones` vs `api/v1/zones`, invented VM actions). Do not copy endpoints from it.

Partially verified against a live account — see *Live-check results* at the end. Items marked **live-check** must be confirmed before we rely on them in the demo.

## Basics

- Base URL: `https://api.iran.liara.ir` (CLI default for Iran region; spec lists `https://api.liara.ir`, both are used by the CLI).
- Auth: `Authorization: Bearer <api-token>` (token from console.liara.ir/API). Team context: `?teamID=<id>` on every request (verified live 2026-10-23; `LIARA_TEAM_ID`). In team mode `/v1/me` adds a `team` object with `balance`, `minCreditAmount` and the member's role/permissions; `/v1/billing` still reports the *personal* account. Documented base URL: `https://api.liara.ir` (developers.liara.ir; `api.iran.liara.ir` answers identically). Networks are separate per team/personal context.
- Other hosts: DNS/mail/object-storage use separate services (`api/v1/...` paths), IaaS uses `https://iaas-api.liara.ir`, AI uses `https://ai.liara.ir`.

## PaaS (apps) — what we need for the MVP

Risk tier: **R** = read-only, auto-run · **S** = sensitive, needs user confirmation.

| Capability | Endpoint | Tier | Notes |
|---|---|---|---|
| List apps | `GET /v1/projects` | R | |
| App details (status, plan, envs, scale) | `GET /v1/projects/{name}` | R | env values included → mask secrets in UI/LLM context |
| Create app | `POST /v1/projects` `{name, planID, platform, bundlePlanID, network?, readOnlyRootFilesystem?}` | S | costs money |
| Delete app | `DELETE /v1/projects/{name}` | S | destructive |
| Start / stop | `POST /v1/projects/{name}/actions/scale` `{scale: 1\|0}` | S | |
| Restart | `POST /v1/projects/{name}/actions/restart` | S | |
| Change plan (resize) | `POST /v1/projects/{name}/resize` `{planID}` | S | billing impact |
| Available plans | `GET /v1/me` → `plans.projects[*]` (filter `available`) | R | no dedicated plans endpoint in CLI; also `GET /v1/me/subscription` |
| Set/unset env vars | `POST /v1/projects/update-envs` `{project, variables:[{key,value}]}` | S | **replaces the full list** (CLI merges with current envs first); triggers restart |
| Deploy: upload source | `POST /v2/projects/{name}/sources` multipart `file` (tar.gz, ≤256 MiB) → `sourceID` | S (with release) | |
| Deploy: create release | `POST /v2/projects/{name}/releases` `{sourceID, port, type}` → `releaseID` | S | |
| Release status | `GET /v1/releases/{releaseID}` → `state`: `BUILDING/PUSHING/DEPLOYING/READY/FAILED/UNHEALTHY/CANCELED` | R | used to verify a deploy |
| Build logs | `GET /v2/releases/{releaseID}/build-logs?since=` | R | `buildOutput[]` with `stream` STDOUT/STDERR |
| Cancel build | `POST /v2/releases/{releaseID}/cancel` | S | |
| Release history | `GET /v1/projects/{name}/releases?page=&count=` | R | has `currentRelease`, state, gitInfo, tag — **no `sourceID`** |
| Runtime logs | `GET /v2/projects/{name}/logs?start=&direction=forward&last_lines=&releaseId=` | R | CLI form (current). Spec still documents `GET /v1/projects/{name}/logs?since=` — prefer v2, **live-check** |
| Events (restarts, OOM, deploys…) | `GET /v1/projects/{name}/events?start=&end=&search=` | R | key input for diagnosis — **live-check** event shapes |
| Metrics summary | `GET /v1/projects/{name}/metrics/summary` | R | cpuUsage, memoryUsage, disksUsage |
| Metrics series | `GET /v1/projects/{name}/metrics/{cpu\|memory\|network-receive\|network-transmit}?since=` | R | Prometheus-like `[ts, value]` per applet |
| Applets (instances) | `GET /v1/projects/{name}/applets` | R | |
| Toggles | `POST /v1/projects/{id}/{zero-downtime\|default-subdomain\|fixed-ip\|read-only}/{status}` | S | |
| Update port | `POST /v1/projects/update-port` | S | CLI only, not in spec |
| Disks | `GET/POST /v1/projects/{name}/disks`, resize, backups, FTP | R/S | |
| Domains | `GET /v1/domains?project=` (also used by the CLI), `POST /v1/domains` {name, project, type: PROJECT}, `POST /v1/domains/set-project`, `POST /v1/domains/provision-ssl-certs`, `POST /v1/domains/{id}/ssl/disable`, `POST /v1/domains/{id}/set-redirect`, `DELETE /v1/domains/{id}`, `GET /v1/domains/{id}/check` (status, TXT `verificationCode`, record checks `CNameRecord`/`acmeRecord` = e.g. `INVALID`; no target values — those are only in the console) | R/S | Lia: list R; add/remove S |

## Other services (out of MVP scope unless needed)

- **DBaaS** `/v1/databases…`: list/get/create/delete, scale, resize, backups (create/list/download), settings, public network, metrics (cpu/memory/network/summary), events.
- **Account**: `GET /v1/me`, `/v1/me/subscription`, `/v1/me/free-credit`, `GET /v1/billing`.
- DNS zones, object storage, mail, IaaS VMs, file browser, database inspector — specs exist in the openapi repo.

## Liara AI (not used)

`https://ai.liara.ir/api/{workspaceID}/v1/chat/completions` — **OpenAI-compatible**, spec says it supports streaming, **tool calling** and multi-modal input; `GET /api/{workspaceID}/v1/models` lists models. Lia does **not** use it: the challenge rules forbid Liara AI, so the LLM is a separate OpenAI-compatible provider (default boxapi).

## NOT available via API (must be reported to the user as limitations)

- **Rollback to a previous release**: no endpoint. Releases don't expose `sourceID` and there is no documented "list sources" endpoint. Workaround: our agent records the `sourceID` of every deploy it makes and re-releases an old one (`POST /v2/projects/{name}/releases {sourceID}`) — **live-check** that old sources are retained. Otherwise rollback = redeploy old code from git.
- **Horizontal scaling**: `actions/scale` only accepts 0/1 (on/off); no replica count.
- **Shell / exec**: `liara shell` uses a websocket to the app, not a REST endpoint — out of scope.
- **Alerts / notifications config, health-check config, cron/scheduled jobs**: no endpoints found.
- **Per-request HTTP metrics / error rates**: only CPU, memory, network, disk. HTTP 5xx detection must come from logs or our own probing of the app URL.
- **Log search/filter server-side**: logs are fetched by time window; filtering is ours.
- **Plan prices**: only plan IDs/availability via `/v1/me`; **live-check** whether price fields exist.

## Live-check results (2026-10-22, read-only)

- Token works against `https://api.iran.liara.ir` (`GET /v1/projects`, `/v1/me`, `/v1/databases` → 200). Account has no apps/DBs yet.
- **Plan prices exist** in `GET /v1/me` → `plans.projects[id]`: `price` is **hourly**, `displayPrice` monthly (toman); also `RAM.amount` (GB), `CPU.amount`, `volume`, `region`. Use `*-g2` plans (cheaper). E.g. `small-g2` 0.5 GB/0.5 CPU ≈ 660/h, 475,000/month; `medium-g2` 1 GB ≈ 1,146/h.
- `plans.projectBundlePlans.g1.default` (price 0, available): `rollback: true`, `maxReleaseRetention: 10`, `maxLogsRetention: 7` (days), `buildTimeout: 60`. So Liara retains old releases and supports rollback in the console — re-release by old `sourceID` is the likely API path (still to verify on a real app). `default` applies to legacy (g1) plans only.
- **g2 plans have their own feature bundles**: `plans.projectBundlePlans["small-g2"]` = `free` (unavailable) / `basic` / `standard` / `pro`. The bundle's `price` is what is billed — the CLI shows only the bundle price. `small-g2`: basic ≈ 382/h (275k/month, rollback, 7-day logs, 5-min build timeout), standard ≈ 660/h (= the plan's own `price`), pro ≈ 1,146/h. Lia defaults to `basic`.
- **`POST /v1/projects` on a g2 plan without `network` → `400 g2_plan_unavailable`** (tried with `bundlePlanID` `default` and `basic`; no app was created). The CLI requires a network (`GET /v1/networks`, create with `POST /v1/networks {networkName}`) and passes its `_id` as `network`. This account has no networks yet; network pricing is not exposed by the API (user is checking).

- **2026-10-23:** `POST /v1/networks {networkName}` → `{network: {_id}}` (worked, no balance needed). `POST /v1/projects` with that network → `402 Not enough balance` when `GET /v1/billing` → `user.balance` (0) is below `user.minCreditAmount` (20,000 toman). Lia now checks this before create/resize. **That 402 was caused by calling without `teamID`** (personal balance 0; team balance is funded). With `teamID`, `lia-net` + `lia-demo` were created; `GET /v1/projects/{name}` returns `hourlyPrice` + `bundleHourlyPrice`. g2 hourly cost = plan `price` + bundle `price` (small-g2 659.72 + basic 381.94 ≈ 1,042 toman/h).

## Live-check results (2026-10-23, on `lia-demo`, team context)

- **Deploy:** upload source → `POST /v2/projects/{name}/releases` → poll release `state` until `READY`. Flask must listen on port **80** (CLI `get-port.ts`); the platform runs its own gunicorn (20.1.0) behind nginx.
- **READY ≠ serving.** The new container logs its first line ~10 s after the release is created; the old container keeps answering until ~22 s. Lia waits for the new release's first log line, then 15 s, then probes the site. Env changes (`update-envs`) trigger a new release the same way.
- **Logs:** `/v1/.../logs` is dead ("Please upgrade your client"). `/v2/projects/{name}/logs` takes `start`, `direction`, `releaseId` (filter works; `limit`/`since` → 400). Response `data[]` = `{metaData: {releaseId}, values: [[ns_ts, '{"type":"stdout|stderr","_entry":"..."}']]}`. Diagnosis reads only the current release's logs — older crashed releases otherwise mislead the LLM.
- **Events:** `GET /v1/projects/{name}/events?start&end` → `{data: [{id, status, action, time, release, tag}]}`, container lifecycle (`create/start/kill/die/stop`) per release tag.
- **Metrics summary:** `{cpuUsage, memoryUsage, disksUsage}`, each a list of `{value: [ts, "bytes-or-cores"], applet}`, one entry per container (old + new during a rollout).
- **Rollback works:** re-releasing an old `sourceID` (from Lia's local deploy log) brought back v1.0. No rollback endpoint exists for releases Lia didn't deploy.
- **Restart:** `POST /v1/projects/{name}/actions/restart` works; the site is back within ~20 s.
- Port is not on the project object; read it from the current release.
- `GET /v1/projects/{name}/applets` → 500.
- **End-to-end via the agent (verified):** v2 deploy → 500 → `diagnose_app` → ranked causes (`KeyError: 'DISCOUNT_PERCENT'`, high) → `set_env` with confirmation → rollout wait → verified 200.
