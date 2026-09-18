# RIG + AxE Pilot — Source Audit and Readiness

**Audit date:** 2026-09-18. **Scope:** code-grounded specification work, not an implemented pilot. **Tracking:** [pm-cli #30](https://git.hyperide.ai/ultrabricks/pm-cli/issues/30). **Master specification:** [open in spec-web](https://spec-dev.hyperide.ai/spec/rig-axe-pilot-master-spec).

## Sources actually inspected

| Component | Observed revision | Inspection and limit |
|---|---|---|
| AxE / Telos | `0f3e99779206159ee0aa3601ee4e9bc010bf53eb` | Repo rules; Telos concepts/store migration from the preceding analysis; actual backend, UI routes, CLI and signal declarations; native telos-service targets. No native integration test executed in this audit. |
| pm-cli | `386cefe05a7880b3080c2cba5b8a335f43e2838f` | README, rules, task and Telegram adapters; 40 existing adapter tests executed in an isolated worktree. No production journal read or changed. |
| task-cli | `c663520e6335b90e16d0f16fc9df4efafc3db89a` | Model and backend protocol from the preceding analysis; present root contains unrelated untracked tasks-app adapter work, which was not modified or counted as merged functionality. |
| tasks-app | `501cce5568ec1137458bb012fe968150fe21583f` | Committed OpenAPI, package manifest and integration service read using git show. Shared working checkout is dirty and was not modified. Source state is not deployed-state verification. |
| tg-cli | `469033fe2c768542e55d832d97502de375598f87` | README machine-readable message recall, transport and report seams; no live private conversation read or Telegram message sent. |
| harness-cli | `6a9ae0673db13881c91c6fbc27f3e1c8270adc84` | README confirms local process tasks and state directory; no harness worker dispatched. |
| rig-cli | `4117fb562163238ffa7f0b397d0ee8e3e18ec20b` | Source revision recorded; no provisioning/reconcile action executed. |
| review-cli | `156f4ad5d4e0ba3d71a65102a7eefefce652bda7` | README, live help and spec-web lifecycle/publication commands; existing daemon observed. Review results are recorded separately below when available. |
| minigraf | remote HEAD `5721d6611894c938d6c62a3784da7c62a81d7f24` | README read through fj; git ls-remote confirmed HEAD. No checkout build, bindings, persistence, benchmark or concurrency behavior tested. |

## Findings that change the design

**PM onboarding is not synchronization.** `pmlib/adapters/task.py::plan_ingest` ignores a ticket already present in the projected task references. This is deliberate in the current implementation and means later status/description updates need a separate reconciliation path. Related existing issue: [state refresh, pm-cli #3](https://git.hyperide.ai/ultrabricks/pm-cli/issues/3). Partial-fetch visibility is related to [pm-cli #4](https://git.hyperide.ai/ultrabricks/pm-cli/issues/4).

**Telegram semantic identity is underspecified in the current PM adapter.** `plan_capture` constructs `tg:<id>` from the returned message ID, without bot/chat identity. This is a source-level gap for multi-chat use, not a claim that an actual user's message has already been lost. The master spec requires scoped identity and forbids guessing missing scope.

**tasks-app has usable foundations but not a proven Telos integration.** The observed OpenAPI exposes cookie and bearer authentication plus issue/comment/relation/document/attachment operations. The integration service persists inbound webhook deliveries with scoped deduplication and contains an outgoing outbox path. It also has GitHub-specific merge-to-Done behavior. None of that demonstrates a generic Telos feed or evidence-gated completion for pilot-managed issues.

**Telos is not merely a static proposal.** The Python backend calls a real AxE HTTP client and reads basis-stamped CLI state; the UI exposes document/frontier/staged/ideas/decide/node routes; telos-service declares Containium generation/readiness/dev targets. Actual packaging, runtime admission, lease semantics and end-to-end behavior still need P0/P3 evidence.

**minigraf is embedded.** Its README explicitly excludes a server/client-server core and describes durable covering-index sidecars. The pilot therefore does not invent a minigraf network service or back up only a casually selected `.graph` file. The engine's speed campaigns are not treated as achieved pilot performance.

## Checks actually executed

The existing PM adapter baseline was run in `/Users/ultra/xp/pm-rig-axe-pilot-spec-20260918` with `python3 -m pytest tests/test_adapter_task.py tests/test_adapter_telegram.py -q`: **40 passed in 0.04 seconds**. These tests validate existing adapter behavior, including its current limitations; they do not prove the proposed integration, crash recovery or container security.

Before registering the new documents, `review spec-web status` reported the existing daemon at port 7920. HTTP GET probes returned 200 for `http://127.0.0.1:7920/` and `https://spec-dev.hyperide.ai/`. Those initial probes prove the existing navigator was reachable, not that the new specification was already registered. Document-specific checks are recorded in the final publication receipt.

The local `docker version` command returned `0.5.2 / {{if .Server}}0.5.2{{end}}`, and `docker compose version` returned `Mocker Compose version v0.5.2`. This does **not** establish a working Docker Engine or enforced Compose isolation. The audit does not infer whether this is an intentional compatible runtime, a wrapper or a simulation. P0 must identify and test the actual runtime before connected execution; no container was started by this audit.

## Not tested or authorized by this audit

No native AxE build, minigraf benchmark, Linux/ARM64 image, cross-process lease race, tasks-app integration mutation, production database import, bot receiver change, agent dispatch, model-assisted implementation, merge or deployment was performed. No new runtime credential was read or issued. Existing dirty worktrees and unrelated processes were left alone. The source-code baseline is not a statement about the status of parallel work in other sessions.

The document was authored in the isolated branch `docs/rig-axe-pilot-master-spec-20260918`, based on PM source `386cefe05a7880b3080c2cba5b8a335f43e2838f`. The master contract stays separate from this dated audit so that intended behavior is not confused with a session diary. Tracking issue pm-cli #30 covers only this design and publication work.

## Tool and process reliability

Some combined remote commands were rejected with an undetermined safety status. Narrow, explicit read operations and the single isolated-worktree creation succeeded; no credential extraction, disabled hook, obfuscation or privilege escalation was used. `fj issue list` does not exist on the installed CLI; help identified the supported `fj issue search` command. The spec-web directory under `~/.config/review-cli/spec-web` is persistent review state, not the source-code repository.

For review processes, stdin must be redirected from `/dev/null` when no input is intended; otherwise the remote command's open input channel can leave review-cli waiting before reviewers launch. Record only owned PIDs with command names and elapsed time when diagnosing; full argv may contain prompts. Use explicit file paths for staging, retain the master document's digest with review receipts, and do not claim review completion while a job is still running.

## Publication and review receipt

The initial master and evidence documents were registered in spec-web and the master was opened in the operator browser. Chromium verified the external master page, all 22 main sections and its spec/comments/drafts/events API responses; local desktop and mobile rendering had no reported page errors. The initial full PM baseline passed 304 tests and ruff. The initial architectural panel returned six successful reports from nine routes, with three failures including exhausted provider quota; its generic code-diff review was not counted as architectural acceptance. Those reports motivated concrete recovery/fencing/completion corrections. They do not constitute review of later edits or runtime readiness. The current Danny/TypeSafe revision and its new verification receipts are recorded below.


## Danny and TypeSafe revision — 2026-09-18

The provided JSON export was read and matched to all three submitted remarks in the actual spec-web store. Each was answered through the supported reply command with `--no-tg`; the live store now reports three answered comments with one reply each, not reviewer-resolved comments. The original export reports that automatic delivery to tmux target `rig` failed; reading the durable store recovered the remarks without inventing a live agent session.

The revised master removes the earlier mandatory tasks-app issue authority for native Telos work. Its canonical nodes now drive task-cli commands, task/feature projections, verification and recovery. A feature catalog links backend/frontend/marketing views to spec headings and independently evidenced implementation/release dimensions. VO is the default for new internal protocols and durable records, with external JSON confined to adapter edges. These are design corrections, not implemented product changes.

Additional source inspection at AxE `0f3e99779206159ee0aa3601ee4e9bc010bf53eb` covered `derive.py` frontier ordering/unlocks, `model.py` attribute validation, `88-literate-spec-linking.md`, the three decision-mode specifications, the `ResumeMode` source type, `containium/09-wire-format.md` and `vo/03-schema-identity.md`. Concrete caveats: frontier ordering prose and current Python code differ; open descriptive attributes do not gain control semantics; VO client/Python/gateway coverage is explicitly partial; some older decision-function examples conflict with the current mode descriptions. No native conformance suite was run to resolve those upstream discrepancies.

Official TypeSafe documentation and public JavaScript SDK source were reviewed, including API answer shapes, confidence semantics, Jev limitations, model versions, external service/privacy boundaries, response casting and retry/debug logging. The dedicated research note links the sources and separates their claims from the proposed Op integration. No TypeSafe API credential, hosted inference, model benchmark or private-source upload was performed. The core pilot remains unimplemented.

### Current document checks and baseline

The unchanged PM source baseline was rerun in the isolated worktree: `python3 -m pytest -q` reported **304 passed in 1.32 seconds**; `python3 -m ruff check .` and the staged whitespace check passed. These are existing PM tests, not executable tests of the proposed Telos/TypeSafe integration.

The external spec-web master, TypeSafe research and Danny-disposition pages each returned HTTP 200 and rendered their expected contents in Chromium, with 22, 8 and 5 main sections respectively and no reported page errors. Desktop and narrow-screen screenshots were inspected; the current master exposes the new TypeSafe section and three answered review threads. Original quoted text changed in the revised spec, so a former quote may be shown as unanchored while its original ID, section context and full answer remain intact. Reviewer content was not rewritten merely to hide this state.

Publication check records and screenshots are in `/tmp/rig-axe-danny-20260918/publication/`; they are local audit evidence, not a production availability guarantee. Both new companion documents are registered in the existing spec-web daemon. No new daemon, bot receiver, placeholder tmux session or recurring task was installed.

### Independent review and correction binding

Two explicitly selected architectural reviewers completed successfully on master SHA256 `a65970de2713a42cc31dc080fe918d642826277af88383933823bcf73f26ff38`. Their reports are preserved in `docs/reviews/rig-axe-danny-typesafe-architecture-review.md`. Findings led to a named release/deployment observation owner, a shared versioned feature-verification predicate, explicit generated Python/TypeScript VO dependencies, reuse of the existing integrations outbox, route-inventory tests and current original-principal authorization for pending commands.

One suggested correction was deliberately rejected: a previously admitted command must not become historically rejected merely because authorization later expired. The specification instead distinguishes authorized lookup of an immutable admission receipt from admission of an unseen command and permission for future effects. The focused recheck evaluates this distinction. No review report is counted as an executed integration test.

### Actual comment receipts

| Source comment ID | Supported command receipt | Verified state |
|---|---|---|
| `1d5c980c6c26434891baa1e69829c8c8` | `[review spec-web reply] replied to 1d5c980c6c26434891baa1e69829c8c8 (now answered)` | One reply; answered, not resolved. |
| `b841f96533aa4b059844a08798c74dac` | `[review spec-web reply] replied to b841f96533aa4b059844a08798c74dac (now answered)` | One reply; answered, not resolved. |
| `cf4b624703c6419286644707b27c1061` | `[review spec-web reply] replied to cf4b624703c6419286644707b27c1061 (now answered)` | One reply; answered, not resolved. |

Each command used the canonical master path and `--no-tg`. The original replies remain in the existing spec-web store; this audit contains their command receipts rather than a claim that a new communication channel was installed.

### Artifact identity after corrections

| Document | SHA256 |
|---|---|
| `rig-axe-pilot-master-spec.md` | `e4414d352fa6f6292a8248e5e8dab7f19c6292626c17cce5e70eb72c9d5ec83c` |
| `rig-axe-typesafe-decision-research.md` | `cab6b28cc1485fe275757beb5572b6f382c95068baa3e2ba734a4b35e461d885` |
| `rig-axe-pilot-danny-review.md` | `7a4f9de9adb5390dcef63606a22bd8374616fc1eeb715f5e6c3b2f35e758e87f` |

These digests bind the exact corrected documents to this audit. The evidence record does not contain a self-referential digest of itself; its Git blob/commit records its own identity. Later corrections require a new digest entry and do not retroactively change the reviewed revision.

### Focused recheck and final correction receipt

The focused recheck completed all three roles of the selected Sonnet model; it is not represented as three different independent models. The reports are retained in `docs/reviews/rig-axe-danny-typesafe-focused-recheck.md`. They accepted the main corrections and requested smaller clarifications. Final text now names the receipt viewer's own read permissions, distinguishes mutable DeliveryRecord from immutable AdmissionReceipt, states that P0's codec exit receipt is P1's entry prerequisite, checks execution-capability expiry separately from submission validity, and names the release-observation verifier. These final edits were author-checked, not sent through an additional review round.

Final master SHA256: `cbcacc931a5009b52d9315aa4893573f604e1a7949cf300406fa1202578c1b52`. Earlier hashes above identify earlier review boundaries, not the final text. The research and Danny-disposition documents are unchanged from their recorded hashes.

Exact reply-body identity captured from the live store:

| Comment ID | SHA256 of first reply body |
|---|---|
| `1d5c980c6c26434891baa1e69829c8c8` | `eca4b84fc50f36c3bf1d9ae411d5adbaf70f8def5a768a83ed24deee33c26abc` |
| `b841f96533aa4b059844a08798c74dac` | `72b26eccbabef3375aabf0e30cb27a1bfb6e89c8ffa0d8b62e27908bcd7b2353` |
| `cf4b624703c6419286644707b27c1061` | `e24f6a03c2559eb151f1268a70f6bf0dd0ec0af0de9f6439fbc7895ca5a47da9` |
