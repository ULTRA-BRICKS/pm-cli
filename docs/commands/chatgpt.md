# ChatGPT Guardian controls

`pm chatgpt` and `pm chatgpt status` invoke the installed
`chatgpt-guardian --workspace home status`.
This is an observation of the existing Guardian, with no browser launch, recovery send,
or pm state/event writes. Guardian itself owns its database and may initialize it on
first access. This command does not install or start Guardian.

Install the reviewed `agenttools-chatgpt` package so `chatgpt-guardian` is on PATH.
Put `--workspace home` or `--workspace glide.vc` before the action to select the
account. pm always forwards the workspace explicitly before Guardian's action;
the default is `home` (the interactive TUI defaults to all accounts, see below).
When `--state` is omitted, pm sends no state argument:
the installed Guardian resolves its workspace configuration (including `state_path`)
and defaults. pm does not discover, migrate, or open databases. Install a Guardian
version with workspace configuration support to use that selection. To share an
existing Guardian/HA owner's database, use Guardian's `CHATGPT_GUARDIAN_STATE`
override or `--state` before the action. An explicit path is forwarded as one literal
`--state=VALUE` argument for status and all durable controls. An overridden database
must belong to the selected account: Guardian rejects an account_scope mismatch
rather than silently reporting another account's status.

```sh
pm chatgpt --state /path/to/shared.sqlite status
pm chatgpt --workspace home status
pm chatgpt --workspace glide.vc status
pm chatgpt --workspace glide.vc --state /path/to/glide.sqlite status
pm chatgpt --workspace glide.vc watch https://chatgpt.com/c/example --apply
pm chatgpt --workspace glide.vc stop --chat '*' --apply
pm chatgpt --workspace glide.vc resume --chat '*' --apply
pm chatgpt watch https://chatgpt.com/c/example --apply
pm chatgpt unwatch https://chatgpt.com/c/example --apply
pm chatgpt stop --chat https://chatgpt.com/c/example --apply
pm chatgpt resume --chat https://chatgpt.com/c/example --apply
pm chatgpt stop --chat '*' --apply
pm chatgpt sol-opt-in https://chatgpt.com/c/example --apply
pm chatgpt sol-opt-out https://chatgpt.com/c/example --apply
```

Every control change requires `--apply`. Stop/resume require an explicit `--chat`;
`'*'` targets all chats. Watch/unwatch and Sol consent require exact permanent chat
URLs. pm validates all chat URLs before delegating: only `https://chatgpt.com/c/<id>`
with ASCII letters, digits, `_` or `-` in the ID and an optional trailing slash is
accepted. Queries, fragments, escapes and option-looking targets are rejected with
exit code 2 without invoking Guardian. Watch is a durable enrollment control,
not a polling command. Sol consent does not switch model or effort.

`--apply` authorizes only the requested control change; it is never forwarded as
Guardian's recovery activation flag. Recovery remains managed by the existing owner,
which requires separate explicit activation, account-verified UI contract, manual
login/challenge handling, and per-chat consent. Keep one owner per dedicated profile.
No run/init, arbitrary argument passthrough, credential options or quota-clear are
exposed. Use the installed Guardian's help for independently managed features.

Guardian stdout/stderr and normal exit codes pass through unchanged. Signal exits
use conventional `128 + signal`; missing executable returns 127, execution failure
126, and a 30-second timeout 124. A timeout can follow a completed control write:
inspect status before retrying. pm neither reads nor prints authentication tokens.

## Permanent recovery service

Use `pm chatgpt --workspace glide.vc service` (or `service status`) to observe the
installed systemd **user** service. This defaults to read-only status. Workspace
`glide.vc` maps only to `chatgpt-guardian-glide.service`; `home` (the default) maps
only to `chatgpt-guardian-home.service`. Arbitrary unit names are not accepted.

```sh
pm chatgpt --workspace glide.vc service
pm chatgpt --workspace glide.vc service status
pm chatgpt --workspace glide.vc service start --apply
pm chatgpt --workspace glide.vc service stop --apply
pm chatgpt --workspace glide.vc service restart --apply
pm chatgpt --workspace home service status
```

Every lifecycle mutation requires an explicit `--apply`. These commands submit
`systemctl --user --no-block start|stop|restart` for the fixed unit, then observe
its state with `systemctl --user --no-pager show` and only the `LoadState`,
`ActiveState`, `SubState`, and `Job` properties. They use literal argument arrays,
with no shell. The output reports observed state; an accepted request alone does
not establish that startup, shutdown, or recovery has completed. A queued job or
transitioning state is reported as pending (exit 3). Failed/unavailable or
unrecognized observed state returns 1; a unit reported as not installed returns 4.
A nonzero systemctl exit is preserved (signals use `128 + signal`) and leaves the
service state unconfirmed. Service status prints only validated state properties;
raw systemctl output is not printed.

Each subprocess wait is bounded to 10 seconds (a mutation followed by status can
wait up to 20 seconds total). Missing systemctl returns 127, execution failure 126,
and timeout 124. A timeout leaves the client and service un-killed; its request may
still be pending. Inspect `service status` before retrying a mutation.

The runtime unit must be installed separately by deployment. pm honestly reports
missing units and does not install, enable, or launch an additional daemon. The
unit runs the shared agent-tools Guardian library, which owns all service logic,
recovery policy, profile, and database access. pm does not open a database or
browser for these service controls. `--state` is rejected with exit 2 because it
cannot change an installed unit's configuration. Existing `pm chatgpt stop --chat
... --apply` and `resume --chat ... --apply` retain their durable chat-state meaning;
`service stop --apply` instead stops the service. All help paths avoid subprocesses.

## Service logs

```sh
pm chatgpt --workspace glide.vc logs
pm chatgpt --workspace glide.vc logs --lines 100
pm chatgpt --workspace glide.vc logs --follow
pm chatgpt --workspace home logs --follow --lines 25
```

The default command is exactly:

```sh
journalctl --user --unit chatgpt-guardian-glide.service --lines 50 --no-pager
```

It shows the last 50 entries and exits, with a 10-second wait limit. `--lines`
accepts integers from 1 through 500. `--follow` appends only `--follow` to the
argument list and streams until interrupted; this explicit streaming mode has no
overall timeout. Ctrl-C returns 130, requests termination of only the journal
reader, and waits at most 10 seconds without force-killing it. The service is
unaffected. A default-read timeout returns 124 without force-killing the reader.
Missing journalctl returns 127, execution failure 126, and nonzero journalctl exits
pass through (signals use `128 + signal`). Journal output is displayed directly;
it can contain sensitive application logs. An empty journal does not prove that
the service is installed or running; use `service status` to check.

Logs use the same fixed workspace units as service controls, with literal argument
arrays and no shell or arbitrary journal options. They require no `--apply` and
reject `--state` because a database path cannot select a systemd journal.

## Installation expectation

Deployment must install the reviewed shared `agenttools-chatgpt` package from
agent-tools (`lib/agenttools_chatgpt`), providing the `chatgpt-guardian` console
entry point (`agenttools_chatgpt.cli:main`) on PATH. Its workspace configuration
owns `state_path`. Deployment must separately install the final systemd user units
`chatgpt-guardian-home.service` and `chatgpt-guardian-glide.service` with the
appropriate account/profile and Guardian configuration. systemctl and journalctl
must be available with access to the user's manager and journal. pm service
start/stop/restart does not install or enable units, or modify their configuration.
These commands describe the expected installed interface; this change does not
establish that any runtime package or unit has been deployed.

## Accounts, session progress monitor and interactive TUI

```sh
pm chatgpt accounts            # supported accounts: configured?, service, monitor counts
pm chatgpt accounts --json
pm chatgpt tui                 # all accounts at once (default without --workspace)
pm chatgpt --workspace all tui
pm chatgpt --workspace glide.vc tui
pm chatgpt --workspace all monitor --json
```

`accounts` lists `home` and `glide.vc`. *Configured* means the fixed systemd user unit
is installed (`systemctl --user show`, read-only, 5 s bound); Guardian databases are
never opened or initialized. When the unit probe or the monitor telemetry cannot be read,
the account shows `unknown`/`unavailable` with the error and its counts are `unknown`
(JSON `null`), never 0 or stopped. `accounts` rejects `--state` and a single `--workspace`.

`--workspace all` is accepted only by `monitor`, `tui` and `accounts`; every other command
(status, controls, service, logs) rejects it with exit 2, and `all` is never forwarded to
Guardian or systemctl. `--state` cannot be combined with `all`. Without `--workspace`,
the interactive TUI shows all accounts; plain `monitor`/`monitor --json` and all other
commands keep the `home` default. Accounts are read concurrently: one failing account
shows `<account>: UNAVAILABLE <error>` and never hides the other. Every row and event is
labelled `[account]`; `monitor --json --workspace all` returns
`{"workspace":"all","accounts":[...],"sessions":[...],"events":[...]}` (per-account schema
v1 snapshots stay unchanged; exit 1 only if no account could be read). The same chat ID in
two accounts is two distinct targets: an action always carries the row's own account.

Reading a snapshot runs `chatgpt-guardian ... monitor --json` without a shell, with stdin
closed, in its own process group. Stdout and stderr are each read incrementally and capped at
1 MiB; exceeding a cap, or the read timeout (`PMLIB_MONITOR_TIMEOUT`, default 10 s), kills the
reader's process group (descendants that stay in it die with it; one that deliberately starts
its own session is not tracked), reaps the reader and reports the account as unavailable (a
complete snapshot whose exit status lags the deadline gets up to 0.5 s to finish). Event fields are
all optional, but when present they must be well typed: text `kind`/`id` (at most 128
characters), text `chat` (at most 2048; service-level events use `""` or omit it, `null` is
rejected), a finite numeric `at` or `null`, and a boolean `baseline`. A snapshot with a
malformed event is rejected as a whole. The Events pane shows `kind` only when it is a plain
identifier, a chat only when it is a recognised permanent chat URL (its last path segment),
`-` for service-level events, and a fixed `(unrecognized)` placeholder for anything else.
Snapshot reading needs a POSIX host (selectors on pipes, process groups); elsewhere it reports
`Guardian snapshot reading requires a POSIX host` instead of crashing. The Guardian services
themselves are Linux systemd units.

Navigation: Tab switches sessions/events; arrows or j/k select a row; Enter shows details;
`/` searches; `f` cycles all/watched/problems; `A` focuses one account (all -> home ->
glide.vc); `r` refreshes (always read-only); Space pauses only the **display** (the header
says "Guardian is NOT paused"); `q` exits.

### Confirmed actions

`a` opens the action menu for the selected row (or focused account):

| key | action | runs |
| --- | --- | --- |
| `w` | watch chat | `pm chatgpt --workspace <acct> watch <chat> --apply` |
| `u` | unwatch chat | `... unwatch <chat> --apply` |
| `p` | Guardian pause chat | `... stop --chat <chat> --apply` |
| `R` | resume chat | `... resume --chat <chat> --apply` |
| `S` | start the account's service | `... service start --apply` |
| `X` | stop the account's service | `... service stop --apply` |

Every action first shows `CONFIRM <action> on [<account>] <chat> y/n`; only `y` runs it,
Esc or any other key cancels with no effect. Actions run `python -m pmlib.cli chatgpt ...`
as an argv list (no shell, 45 s bound, never retried), so they reuse the validated pm
control path; the TUI duplicates no database mutation. There is no bulk (`'*'`) action,
quota/uncertain clear, model switch, go/send, browser navigation, or service restart.

Watch, unwatch, resume and service start need fresh evidence of the target account: they are
refused when the account is unavailable, flagged stale, or its source `generated_at` is
missing or older than 180 s (judged at the moment of the key press, even while the display
is paused or the last refresh failed), when the row is marked `session_stale`, or when the
chat is not listed exactly once for that account ("ambiguous"). Pressing `y` re-checks the
same account and chat against the newest snapshot and refuses if it no longer qualifies;
the confirmed target never changes. Pause and service stop stay available when the account
and chat are precise. Service actions need an explicit account (never `all`). Service stop
stops recovery for that account only; the observer (monitoring) is a separate service and
keeps running, so observed state may continue to update.

`q` does not quit while a confirmed action is still running (the outcome would be lost);
wait for the result. Ctrl-C exits with status 130 and, if an action was running, reports on
stderr that its outcome is unknown.

The result is shown honestly and followed by a read-only refresh: `success` (exit 0, the
request was accepted; observed state decides the rest), `pending` (exit 3), `uncertain`
(timeout or exit 124: the change may still apply; no automatic retry) or `failed`.
Enrollment or an active systemd unit is not proof that recovery is active, and a service
start does not mean all chats are covered. Resume is scoped to one chat: global, quota and
uncertain blocks are not cleared. With a custom `--state` the TUI is read-only, because
actions always address Guardian's default database.

`monitor --json` returns the same persisted evidence for automation. Plain `monitor`
chooses the TUI in a terminal and a text table in a pipe. `--interval` controls display
refresh (2 to 60 seconds), not browser polling.

The header separates enrolled, present and available sessions. Available means distinct chats already open in the configured dedicated browser, not every conversation in the account. A visible `not-enrolled` chat is observable but is not silently authorized for recovery.

Dispatch acknowledgements and confirmed assistant continuation/completion have different counters. Confirmation requires correlated evidence from the following turn; an uncertain dispatch remains uncertain. Recorded observation times are not reconstructed execution times. Details include the actual last check, next deferred check, dispatch time/state, confirmation timestamps and source freshness, all displayed in Europe/Belgrade time. Optional `error_detail` and `session_stale` fields are shown in details when Guardian provides them.

The TUI reads local telemetry only. The separate `chatgpt-guardian ... observer` process supplies that telemetry without restarting or controlling the existing recovery service. Missing data or a failed refresh retains the original source timestamp and is visibly marked unavailable/stale. Baseline historical events are not new notifications, and unchanged events retain stable IDs.

## Owner-approved session control (API, approvals, MCP, connector)

pm exposes one typed control API for ChatGPT Guardian sessions. Reads return telemetry and
receipts. Every write only QUEUES an owner approval; nothing is sent or changed until the owner
taps Approve on a Telegram card posted through the existing `tg-ctl ask --owner-only`. Before
every ask PM probes the same trusted `tg-ctl` with `capabilities --json` and requires exit 0 and a
bounded JSON object `{"schema_version":1,"capabilities":[...,"ask-owner-only-v1"]}`; anything
else (old CLI, timeout, bad JSON, marker missing) fails closed: no ask is started, there is no
plain-ask fallback, and the receipt is `delivery_unavailable`. PM always passes `--owner-only`
and never an `approved` or sender argument, and owner discovery requires a positive (private
chat) `TG_CHAT_ID`; group, negative or zero ids mean no owner is configured. Only a verified
`tg-ctl ask` exit 0 with an `approved` line counts: free text, an `approved` flag, a sender or
recipient argument, a URL or a command are refused (`forbidden_key`). Timeout, rejection, an
unreachable daemon or an ambiguous delivery leave a durable non-success receipt and are never
retried or re-sent automatically.

### CLI (works without any service)

```
pm chatgpt api METHOD --params '{"...": "..."}'   # or --params - to read stdin (64 KiB max)
```

Stdout is always exactly one JSON envelope: `{"ok":true,"result":...}` or
`{"ok":false,"error":{"code":"...","message":"..."}}` (exit 0 or 1). Error codes:
`invalid_request invalid_params unknown_key forbidden_key unknown_method not_found
account_unavailable refused busy unauthorized payload_too_large internal_error`.

| Method | Params | Result |
| --- | --- | --- |
| `accounts.list` | `{}` | accounts report |
| `sessions.list` | `{workspace?: all\|home\|glide.vc}` (default `all`) | `{workspace, accounts, sessions, events}` |
| `sessions.get` | `{workspace: home\|glide.vc, chat}` | `{workspace, account: <health>, session: <row + latest_turn>}`; `latest_turn == turn` (the exact rendered turn) |
| `actions.request` | `{workspace, action, chat?, expected_turn?, reason?}` | `{request_id, status, action, workspace, chat, expected_turn, expires_at, plan_digest, duplicate}` |
| `requests.list` | `{}` | `{requests: [receipt]}` newest first |
| `requests.get` | `{request_id}` | receipt with status, binding fields and timeline |
| `connectors.list` | `{}` | `{connectors: [{connector, target, state, plan_digest, ...}]}` |
| `connectors.request` | `{connector: "guardian", target: "chatgpt"}` | queued receipt plus `connector`, `target` |

`chat` is a bare conversation id (`[A-Za-z0-9_-]`) or the exact `https://chatgpt.com/c/<id>` URL;
it is normalized once to the canonical URL and anything else (other origins, query, fragment,
whitespace, paths) is rejected. Actions: `watch unwatch pause resume continue` (need `chat`) and
`service-start service-stop` (whole account, no `chat`). `expected_turn` applies to `continue`
only and defaults to the currently observed turn. A duplicate of the same
account/chat/turn/action returns the original receipt (`duplicate: true`); a rejected, expired or
uncertain turn is never asked again.

Request status: `queued`, `asked`, `asked_post_uncertain`, `approved`, `dispatching`, `dispatch_queued`, `sent`,
`applied`, `refused`, `rejected`, `timed_out`, `expired`, `invalidated`, `uncertain`, `failed`,
`delivery_unavailable`. A queued receipt is never a completed action.

### Continuation policy

A completed answer is not a completed task, so pm only ever asks. A continuation card is proposed
per account/chat/turn when the chat is enrolled, present, observed currently (the per-session
`session_stale`/`check_stale` flags and `last_checked_at` against its backoff, not just the
snapshot age), non-generating (`complete` or `unknown`), free of recovery blockers, global stop,
quota latch, unresolved send, draft, attachment and auth challenge, has no retracted completion,
and has been quiet for 120 seconds. Running or working always vetoes, as does a contradictory
completed-and-running row. A different or newer turn invalidates an older proposal, before the
question and again right before dispatch. Existing `Thinking failed` recovery stays in Guardian.
The passive monitor never verifies model or effort (`effort` is empty, the model is only visible
trigger text), so neither gates the question and neither is guessed; Guardian verifies them
after approval and refuses fail-closed. The Telegram card is Russian and names the account and
chat, asks the action's own question, states the exact effect and expiry, and says that
declining or ignoring changes nothing.

### Worker and units

```
pm chatgpt approvals run [--once] [--interval 60] [--no-propose]
pm chatgpt unit api|worker        # print a systemd user unit template (never installed by pm)
```

One worker holds a lock (exit 75 if another runs). `--once` runs one tick. Install the templates
manually: `pm chatgpt unit worker > ~/.config/systemd/user/pm-chatgpt-approvals.service`, then
`systemctl --user daemon-reload && systemctl --user enable --now pm-chatgpt-approvals.service`.

### Optional loopback HTTP service

```
pm chatgpt api token-init [--rotate]   # creates <pm state>/chatgpt/api-token (0600); prints the path only
pm chatgpt api serve --port 8792       # POST /v1/rpc with {"method": "...", "params": {...}}
```

Binds `127.0.0.1` only; every request (including `GET /v1/health`) needs
`Authorization: Bearer <token>`; `Origin` headers and non-loopback `Host` are refused; bodies are
capped at 64 KiB and connections at 8. Read the token yourself from the file when configuring a
client; pm never prints it. Template: `pm chatgpt unit api`.

### MCP server

`pm chatgpt mcp` speaks MCP 2025-06-18 over stdio (newline-delimited JSON-RPC). Read tools:
`chatgpt_accounts chatgpt_sessions chatgpt_session_get chatgpt_events chatgpt_requests
chatgpt_request_get chatgpt_connectors`. Request-only tools: `chatgpt_request_continue
chatgpt_request_control chatgpt_request_connector_install`. No tool approves or sends.

A successful `tools/call` returns `structuredContent` = the unwrapped API `result` object and
`content[0].text` = the JSON text of the full API envelope (`{"ok":true,"result":...}`); a failure
sets `isError` and carries the error envelope in both. Tools declare no `outputSchema`.

### Connector

`connectors.request` queues an owner approval of the fixed, reviewed plan: a local launcher,
manifest and onboarding note under `<pm state>/chatgpt/connectors/guardian-chatgpt/`. No
downloads, no network listener, no credentials. After approval the state is `local_ready`, then
`awaiting_platform_connection` once `pm chatgpt connector tunnel-id <tunnel_id>` records the
non-secret tunnel id. It is never reported connected: ChatGPT workspace registration, OAuth and
the platform risk warning are the owner's steps (`pm chatgpt connector` prints them), verified
only by a real ChatGPT tool call.

## Task-completion audit (`pm chatgpt audit`)

A finished answer is not a finished TASK. The audit asks, per conversation, whether the task is
done, not whether the assistant replied. It is read-only: pm never sends to ChatGPT, and Guardian
stays the sole browser sender. The Laya classifier is advisory evidence only; it never alone marks
a task done, sends, or unblocks anything.

```
pm chatgpt audit --workspace home|glide.vc|all|global --days N --turns N --json [--notify] [--include-text] [--playwright-site-packages DIR]
pm chatgpt audit bench [--record] [--json] [--budget SECONDS]
pm chatgpt audit bind --workspace home|glide.vc
pm chatgpt unit audit-service|audit-timer     # print a systemd user unit template (never installed by pm)
```

### Exit codes

`0` every requested account `ok`; `3` worst status `partial`; `4` worst status `unavailable`;
`75` another audit holds the run lock (nothing read; stdout is
`{"schema":1,"status":"busy","reason":"lock-busy"}`); `2` usage error.

### Report and coverage

`--json` prints one report: `schema`, `tool`, `pm_version`, `generated_at`, `days`, `cutoff`,
`workspaces`, `status` (worst across accounts), `classifier` (`status`, `model`, `prompt_version`,
`reason`), `thresholds_digest`, `auto_continue`, per-account `accounts`, `totals` and `notify`.
Each account has `status`, `reason`, `observed_account`, `not_before`, `coverage` (`listed_total`,
`in_window`, `read`, `cached`, `skipped`, `errors`, `partial`, `budget_exhausted`, `stopped`,
`requests`), per-scope `scopes` and `conversations` (newest first, at most 500; `omitted` counts the
rest). Each conversation carries ids, URL, `task_state` (done|incomplete|blocked|working|unknown),
`response_state`, `recovery_state`, `reasons`, `evidence_ids`, `fingerprint` and `source`
(`read`|`cache`); never conversation text (unless `--include-text`) or credentials.

Coverage is honest: an unavailable, stopped or unbound account is never shown as empty or fully
audited. `ok` means every scope completed, no errors, and every in-window conversation was read or
confirmed from an unchanged cache entry; anything missing is `partial`; nothing auditable is
`unavailable`.

### Native read and context expansion

Each conversation is read with the PLURAL endpoint
`/backend-api/conversations/<id>?num_turns=N&include_has_versions=true` (verified live: it read 51
chats in one audit). It is fast, bounds the window to `N` turns and returns a flat, chronological
`messages` list with `page_info.has_previous_page`. The older singular full-mapping endpoint
`/backend-api/conversation/<id>` ignores the turn limit and times out on large chats; its
`mapping` shape is still parsed as a fallback. The reader stamps every payload with the format of
the endpoint it called (`native_format`: `flat` or `mapping`); the flat order is trusted only under
that marker plus order checks (unique ids, `current_node` is the last message, no forward
`parent_id`, user messages never go back in time), and anything unproven is `malformed`, never
guessed. Both shapes produce the same evidence.

A bare "go"/"continue" in a cut window has no goal to judge. The audit re-reads it with 8, then 16
turns (a hard cap), each request still at least the minimum gap apart through the same guarded
reader. If the prior substantive request is still missing at the cap, the evidence stays
`context_truncated` and the verdict is `unknown` (`truncated-context`), never `done`.

### Account binding

Binding is explicit: `pm chatgpt audit bind --workspace ...` records which ChatGPT account the owned
browser belongs to. It is re-verified on every run. A wrong or missing account yields `unknown`
verdicts and an unavailable account (`account-not-bound` / `account-mismatch`).

### Playwright

The installed `pm` entry point already runs under Guardian's venv python, where Playwright is
importable. Otherwise set `PM_AUDIT_PLAYWRIGHT_SITE_PACKAGES` or pass `--playwright-site-packages`.
The directory must be user-owned and not group/world-writable; if not, the account is reported
unavailable with `playwright not installed`.

### Reader contract

The reader reuses an already-open chatgpt.com page of the owned Guardian browser over CDP. It opens
no tab and navigates nowhere. Requests are same-origin fetches with a 15 s timeout, at least 4 s
apart, inside a bounded budget, and stop on 401/403/429 honouring Retry-After (recorded as
`not_before`). The bearer token never leaves the page. Guardian's live service also latches its
own stop on such responses.

### State files

Private (0600 files, 0700 directory) under `<pm store>/chatgpt-audit`: verdict cache, last report,
bindings, `not_before`, notification log and the validation record.

### Classifier route and capabilities

Laya runs in the already-running task-cli classify server; pm never starts a model. A `predict`
without a `model` field is answered by the ENGLISH checkpoint even on the newest socket, so the
socket version says nothing about language support. pm therefore always sends an explicit
`model` and `max_len`: any non-Latin-script letter (Cyrillic, Greek, CJK, ...) in the request or
the final answer selects `multilingual` (`convaiinnovations/laya-multilingual`, `max_len` 2048),
everything else selects `english` (`convaiinnovations/laya`, `max_len` 512, or 2048 for a long
context). Before predicting, pm reads the read-only `op: capabilities` reply (cached for 60 s)
and requires `api_version` 2, the route's entry, the official checkpoint id, `loaded: true` and a
`max_len` window that contains the request. An old server (no `capabilities`), a missing route, a
checkpoint mismatch or an out-of-range window is `unavailable`, and the text is NEVER silently
routed to the other model. An unloaded route gets one content-free warm-up probe (the provider
loads it in the background); that run is `unknown`, pm never waits for a download.

The reply's `meta` is a TOP-LEVEL key beside `result` (a `result.meta` is not accepted). Every
field (`api_version`, `model`, `checkpoint`, `max_len`, `truncated`, token counts) is type- and
value-strict, must equal what was requested and must agree with `result.usage.truncated` /
`state_tokens_dropped`. Missing accounting, a disagreement or a truncated input makes the call
`invalid`, so the verdict is `unknown`, never `done`. A verdict is bound to the ACTUAL
`checkpoint@max_len` of the validated reply, the prompt digest (`prompt_version`) and the decision
threshold digest, not to the socket file name.

Socket choice: `$PM_CHATGPT_AUDIT_LAYA_SOCKET` (an absolute path) wins and is never replaced by
another socket; otherwise the highest-version private `laya-*.sock` under
`$XDG_STATE_HOME/task-cli/classify`. Symlinks, foreign owners and group/world access are refused.
The provider contract needs task-cli 0.23.1; until the runtime `task` CLI is updated from 0.23.0
every classification is `unavailable` (so every model-dependent verdict is `unknown`).

### Cache trust

A cached verdict is reused by the collector only when fresh and it matches the prompt version,
model identity (`checkpoint@max_len`), threshold digest and conversation fingerprint; otherwise the
conversation is re-read. Entries record the native identifiers of the latest turn (`turn_ids`).

### Validation gate and `auto_continue`

Every report and conversation has `auto_continue`, always
`{"allowed": false, "reason": "classification-not-validated"}` unless a matching passing validation
record exists and the verdict fingerprint matches. `pm chatgpt audit bench` runs the fixed
107-case corpus (tune + held-out splits) through the real warm Laya socket and exits 0 only when
it passes (zero false `done` on both splits, zero held-out false `blocked`, held-out exact accuracy
>= 75% and coverage >= 70%, so abstaining on everything cannot pass); `--record` writes
`validation.json` ONLY for such a passing run, bound to the prompt digest, the checkpoint/window
identities, the thresholds+logic digest and the corpus digest, so any change to one of them
invalidates it. Exit 1 = ran but not validated, 2 = classifier unavailable (nothing recorded),
3 = the global `--budget` (default 180 s wall clock, spent on the pipeline system first) ran out:
the report is marked `partial` (`evaluated`/`planned`), `passed` is false and nothing is recorded,
so a timeout can never read as a pass. The clock starts before the provider probe and is checked
between predictions and once more at the end (an overshoot during the last call is partial too);
one in-flight provider call keeps its own 30 s socket timeout and is not interrupted.
pm still never sends; the owner-approved continuation policy above is unchanged.

### Notifications

`--notify` reports significant deltas only, through one-way owner-private
`tg --automated --tag report`, with ids, URLs, states and reasons only. A durable
`delivery_intent` line is written BEFORE the send and an intent with no recorded outcome counts
as uncertain, so a crash around the send can never duplicate it; if the intent cannot be written
nothing is sent. The `tg #<id>` receipt from the `OK` line is stored with the `sent` event and
printed as `receipt`: it is the only proof of delivery. A `delivery_uncertain` delivery is never
replayed; definite failures are retried under a cooldown. Eligibility and intents are claimed
under a non-blocking `notify.lock` in the audit directory: an overlapping run reports `busy` and
sends nothing.

### Poller

The approvals poller skips chats whose trusted fresh verdict is `done`, `working` or `blocked` with
`task-finished`, `task-working` or `task-blocked`. A cached verdict suppresses ONLY when the row's
rendered turn is one of the identifiers the verdict recorded (progress metadata can lag behind a
new request, so timing alone proves nothing), and a `done` verdict additionally needs an `ok`
classification whose validated, non-truncated route meta names the checkpoint and window the entry
is bound to. Otherwise the cached verdict is ignored and the poller behaves as before (a
proposal): doubt never becomes a silent `done`. Everything else is unchanged.

### Units

```
pm chatgpt unit audit-service > ~/.config/systemd/user/pm-chatgpt-audit.service
pm chatgpt unit audit-timer   > ~/.config/systemd/user/pm-chatgpt-audit.timer
systemctl --user daemon-reload && systemctl --user enable --now pm-chatgpt-audit.timer
```

Both commands only print; pm never installs or enables a unit. The timer runs at :00 and :30
Europe/Belgrade (`OnCalendar=*-*-* *:00,30:00 Europe/Belgrade`, `Persistent=false`; the service
audits the last 7 days with low CPU/IO weight and a task cap, the oneshot never overlaps itself and the run
lock exits 75 when another audit is busy). Logs go to the journal (`journalctl --user -u
pm-chatgpt-audit.service`), rate-limited. Optional per-user settings live in
`~/.config/pm-chatgpt-audit/env`: `PM_AUDIT_TASK_LAUNCHER` (an ABSOLUTE path to the installed
`task` launcher, default `~/.local/bin/task`; never a worktree path baked into the template),
`PM_CHATGPT_AUDIT_LAYA_SOCKET` and `PM_AUDIT_PLAYWRIGHT_SITE_PACKAGES`. The service starts the warm
Laya server best-effort with `task classify-server start` (the supported idempotent starter; it
preloads the English model only, the multilingual model loads on the first routed request); pm
itself never spawns a model. Exit codes 3, 4 and 75 do not mark the unit failed.

### Validation results and limitations

Real run (provider 0.23.1, both models loaded, 107 synthetic cases, `pm chatgpt audit bench
--json`; full tables in [classifier-validation.md](../classifier-validation.md)). The hybrid
pipeline (deterministic evidence and precedence + Laya) had ZERO false `done` and zero false
`blocked` on both splits, but it is NOT validated and `auto_continue` stays denied: held-out exact
52%, coverage 43% (abstention 57%), `done` recall 0% (the models rarely clear the 0.85 threshold,
so decidable `done` cases end as `unknown`), `incomplete` precision 79% / recall 73%. The raw
baselines a naive integration would ship are worse or unsafe: the four-way choice prompt had 5
false `done` and 4 false `blocked` on held-out, the per-question prompt 16 false `blocked`. The
earlier independent pilot (raw Laya 3/12 choice-correct, first pipeline 2/8 exact) is kept in
the bench output as a documented counterexample. Only 8 of the 107 cases are routed to the
multilingual model (per-route table in the report), so the multilingual numbers are indicative
only. The two routes are different models with non-equivalent prompts/token budgets, and Laya's
confidence numbers are entropy-like scores, not probabilities: the numbers are not comparable
accuracy estimates and say nothing about general understanding.

- `--workspace global` is an alias of `all` for the audit.
- Project discovery is best-effort: when the project-list endpoint is unavailable (404) the scope
  reason is `unverified` and the account is `partial`, never full coverage.
- A model-dependent `done` needs the classifier to agree and the deterministic gates to hold; with
  the provider unavailable, truncated, old (no `capabilities`) or the route unloaded, the verdict
  is `unknown`.
- Laya is advisory only; the audit never sends, resumes an archived chat, or approves anything.
