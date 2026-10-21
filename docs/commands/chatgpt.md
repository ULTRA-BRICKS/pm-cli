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
