# ChatGPT Guardian controls

`pm chatgpt` and `pm chatgpt status` invoke the installed
`chatgpt-guardian --workspace home status`.
This is an observation of the existing Guardian, with no browser launch, recovery send,
or pm state/event writes. Guardian itself owns its database and may initialize it on
first access. This command does not install or start Guardian.

Install the reviewed `agenttools-chatgpt` package so `chatgpt-guardian` is on PATH.
Put `--workspace home` or `--workspace glide.vc` before the action to select the
account. pm always forwards the workspace explicitly before Guardian's action;
the default is `home`. When `--state` is omitted, pm sends no state argument:
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
