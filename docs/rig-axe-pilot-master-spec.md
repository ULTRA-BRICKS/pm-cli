# RIG + AxE Pilot — Master Specification

**Status:** proposed integration contract; not a claim of implementation or production readiness. **Tracking:** [pm-cli #30](https://git.hyperide.ai/ultrabricks/pm-cli/issues/30). **Audience:** Alex, Danny, implementation agents, reviewers and operators. This document is in English so the same specification can be reviewed by the whole team.

## 1. Purpose and decision

The pilot turns a request into a traceable, evidence-backed result through the existing RIG and AxE ecosystem. It uses **tg-cli for communication; Telos for the pursuit and its planning graph; pm-cli for operator interaction and execution policy; the real AxE event loop and decision functions for durable coordination; and task-cli/tasks-app as independently useful task tools that synchronize with Telos when RIG integration is enabled**. tasks-app keeps its own database, API and complete standalone workflow; integration must not make the OSS product depend on Telos for ordinary use.

The initial environment is isolated and container-oriented. The first complete experiment uses synthetic records and a disposable repository. A later, separately enabled mode connects one allowlisted Telegram conversation and one dedicated tasks-app workspace. No production import, repository merger, bot reconfiguration, merge or deployment follows automatically from accepting this design.

**One sentence:** RIG installs and reconciles the working environment; Telos knows what outcome is being pursued and what work is ready; PM policy running on AxE decides which permitted attempt to launch; workers perform that attempt; evidence flows back to Telos and the task interfaces.

The supplied ecosystem atlas is a historical orientation aid, not an interface specification. This design deliberately moves Telos onto the main planning path, removes the duplicated infrastructure layer, keeps RIG with agent-tools, and does not treat the atlas's ACTIVE labels as deployment evidence.

### 1.1. Terms and product boundaries

This specification defines project-specific terms at first use instead of assuming team folklore. When a term has both a general software meaning and a project-specific meaning, the project-specific meaning below wins in this document.

- **RIG / RIG ecosystem** — the integration and engineering-control layer around the tools: repository/worktree policy, task routing, agent/harness integration, review/verification hooks, environment reconciliation and related operator tooling. RIG is not the product-planning database.
- **AxE** — the runtime/platform that executes typed event/state/decision flows and admitted operations. In this design it is the deterministic coordination/runtime substrate, not another issue tracker.
- **Telos** — AxE's goal/work planning system: pursuits and nodes, dependencies, checks/gates, specification links, calculated ready-work frontier, priority/unlocks and agent briefs. In RIG-integrated mode it owns the graph-specific planning semantics.
- **tasks-app** — the standalone open-source issue/task-management product with its **own PostgreSQL database, API, UI, projects, issues and integrations**. It must remain fully usable without RIG, AxE or Telos. When RIG integration is enabled, selected records are synchronized with Telos through explicit bindings and field ownership; tasks-app does not become a disposable cache.
- **task-cli** — the task command-line client and policy/gate layer. It can operate independently against its supported trackers/backends; in RIG-integrated mode it can route operations through the tasks-app/Telos synchronization contract.
- **Cowshed** — Danny's working name in the 2026-09 coding-agents notes for the deterministic coding-agent coordinator/command surface, illustrated by commands such as `cowshed create` and `cowshed rebase`. The notes do **not** yet establish whether Cowshed is a separate package/runtime or a thin command surface over RIG/AxE; this remains a packaging decision, not an assumed new source of truth.
- **HTS / Hermetic TypeScript** — AxE's pure/deterministic TypeScript tier for agent modules, decide functions, workflow logic and session code. It is TypeScript with nondeterministic/effectful behavior excluded or mediated by checked operations. HTS is **not** the same thing as code intelligence.
- **Op / AxE Op** — a typed effectful operation outside the pure HTS tier: for example network I/O, shell/tool execution or another real-world effect. HTS reaches such effects through the declared Op boundary, so replay/policy can reason about them.
- **Nx** — the monorepo task-graph/build orchestration system used by AxE/Conloca and related repositories. Its project/target/dependency graph determines which declared `lint`, `test`, `build` and other targets are relevant.
- **Nx Op** — the proposed AxE Op that calls Nx programmatically through Nx Devkit APIs, rather than treating a spawned `nx ...` process and its terminal text as the primary interface. It returns structured run/task IDs, states, timings and output references that coordinator state can query.
- **code intelligence** — AST/symbol/reference/type-graph/LSP capabilities used to find relevant code and perform structural edits. It is a separate capability that may be exposed to the coordinator/HTS through Ops; it is not what HTS means.
- **reducer state** — durable structured coordinator state advanced by explicit events/transitions. It is the machine-readable control state; the chat transcript is not the control plane.
- **semantic judge / semantic decision engine** — a bounded model that evaluates a typed question/rubric and returns structured semantic data. **Laya is the default judge for this pilot; Jev is an optional secondary/fallback/comparison backend.** Neither is allowed to replace deterministic checks, permissions or human authority.
- **VO** — AxE's versioned schema/wire/storage discipline for durable internal records and protocol evolution.
- **minigraf** — the graph/query store used for derived relationship/history queries; it is not a replacement for tasks-app's database or Telos/AxE authoritative state.
- **Containium** — the AxE runtime/supervision layer for native generated agent artifacts; it is distinct from Docker/container packaging.

Whenever a new product name, acronym or project-specific primitive is added later, the normative spec must add or link a plain-language definition before relying on it.

## 2. Outcomes and exclusions

A successful pilot proves that a request survives duplicate delivery and restart, produces one canonical work record and its linked views, becomes executable only after its prerequisites and authorization hold, and completes only with evidence tied to the actual artifact. A human can understand its state from Telegram, tasks-app or the CLI without reconstructing an agent session. A failed attempt is visible and recoverable without repeating already completed external effects.

The pilot also measures whether the new workflow reduces human chasing, repeated questions and false completion. Database microbenchmarks and the number of packages moved do not establish that outcome. Performance goals in upstream READMEs remain goals or upstream measurements until reproduced on this workload.

Non-goals are bulk monorepo migration, replacing tasks-app PostgreSQL with minigraf, a new chat application, implementing a second Telos graph in pm-cli, replacing AxE with a Python polling state machine, general-purpose Kubernetes deployment, billing, arbitrary autonomous browsing, training new models, or authorizing real releases. Hero and expanded IAM use have bounded optional lanes; VO and the existing Containium runtime contracts are reused where their boundaries already require them.

## 3. Evidence discipline

The [source audit and readiness record](https://spec-dev.hyperide.ai/spec/rig-axe-pilot-evidence) identifies exact observed revisions, inspected paths, checks actually executed, and unresolved readiness questions. A source declaration proves an interface exists in code, not that it works on this machine. A mock transport proves a fixture, not a real AxE loop. A working Telos UI backed by JSONL does not prove AxE-primary operation.

Every implementation report distinguishes **observed source**, **tested behavior**, **proposed integration**, and **unverified capability**. When documentation and code disagree, preserve the disagreement as a finding, write a focused conformance test, and resolve it at the owning boundary. Do not silently select whichever interpretation makes the demo green.

## 4. System topology

`Telegram → existing tg receiver → scoped integration adapter → AxE admission → Telos pursuit / PM execution agents → worker attempts → review and QA evidence → Telos verdict → tasks-app↔Telos sync + Telegram report`

`RIG + agent-tools` surround this path as provisioning and policy distribution. `task-cli` and `tasks-app` remain independently useful tools; when a workspace enables RIG/Telos integration, an explicit sync adapter binds selected tasks to Telos nodes and synchronizes the mapped state in both directions. `spec-web` exposes this specification and anchored feedback. `minigraf` provides rebuildable graph queries over authorized log data; it is not the transaction owner or a new message broker.

## 5. Ownership: one authority per domain

**Standalone and integrated modes are both first-class.** tasks-app always retains its own durable task database and can be authoritative for a standalone installation. When a tasks-app workspace opts into the RIG/Telos integration, selected tasks acquire explicit bindings to Telos nodes and the two systems synchronize through a versioned contract. Telos owns graph-specific planning semantics such as pursuit/node relationships, readiness dependencies, checks, calculated frontier/unlocks and Telos completion predicates; tasks-app owns its local product state and remains a real writable database, not a cache. Overlapping fields have declared ownership/direction/conflict rules rather than two silent last-writer-wins authorities. A company can deploy tasks-app alone, Telos alone, or the integrated stack.

| Domain | Authoritative owner | Other components may do |
|---|---|---|
| Environment, tool versions and policies | RIG + agent-tools | Report drift; never silently rewrite another owner's policy. |
| Integrated planning graph: pursuits/nodes, dependencies, checks, spec links, calculated frontier/unlocks and planning leases | Telos on its selected AxE backend | tasks-app stores synchronized bindings/copies needed for its product UX and submits changes through the sync contract. |
| Ready work and calculated priority | Telos frontier and its owning rules | Render why/unlocks/boost; PM selects feasible execution without maintaining another ranked backlog. |
| Transactions, typed state, rules, timers and effect decisions | AxE runtime | Consume declared queries/signals, not shadow reducers. |
| Execution policy, attempts and bounded recovery | PM domain agents on AxE | pm-cli commands/renders these agents; it owns no competing scheduler. |
| Satisfaction, evidence confidence and pursuit closure | Telos predicates and authorized admission | Display or request verification; never assign derived Done from a UI field. |
| tasks-app issues/projects/UI and standalone task workflow | tasks-app PostgreSQL/domain model | Operate independently; for bound records synchronize mapped fields/events with Telos while retaining local-only product fields. |
| Integrated feature/planning views | Field-owned tasks-app↔Telos synchronized model | Link nodes, tasks, spec headings and receipts; show sync basis, freshness and conflicts. |
| Released availability by environment/version | Authenticated deployer observations admitted by the release/deployment verifier | Render verified release receipts; never infer availability from merge, code verification or copy. The pilot has no real release writer/credentials and shows not-recorded or explicitly synthetic fixtures. |
| UI preferences, access sessions, task-local metadata and unbound standalone work | tasks-app | Never invent Telos state for an unbound record; enabling integration is explicit. |
| Attempt process/session facts | harness-cli / worker supervisor | Report execution facts; never decide product completion. |
| Message delivery and source receipt | tg-cli transport | Expose a scoped feed; never run a second receiver for the same bot. |
| Anchored specification feedback | spec-web comment store | Import submitted feedback by ID and answer the original thread. |
| Historical and relationship queries | minigraf read projection | Rebuild from authorized logs; never grant execution authority. |
| Artifact bytes and verification receipts | Artifact storage and producing verifiers | Keep immutable scoped references; evidence admission goes through Telos. |

Telos derives the next-work ordering from the graph and its current rules; manual boost is an explicit input, not a manually maintained backlog position. The UI shows what a node helps unlock and why it ranks. The inspected Python `structural_frontier` orders by boost, criticality, urgency, freshness nudge and stable ID; `unlocks` exposes dependency closure. PM must not replace that owner algorithm with a simplistic unlock count or a Jev score. Native/Python ordering parity remains a required conformance check. [S8]

One authority per semantic field does not mean one physical database or one independently deployable product. tasks-app has a permanent first-class database; AxE/Telos has its own durable planning/runtime state; artifacts have their own owner. In integrated mode synchronization uses stable cross-system bindings, source revision/basis, idempotency keys and durable receipts. A tasks-app edit to a Telos-owned graph field becomes a synchronization command and is not considered accepted by Telos until its receipt arrives; a Telos change is mirrored back into tasks-app without erasing tasks-app-only state. Offline changes queue and reconcile. Concurrent incompatible edits become an explicit conflict; no blind last-write-wins overwrite is allowed. Disabling the integration leaves tasks-app usable with the last valid local task data and clearly marks formerly bound Telos-derived fields as disconnected/stale rather than deleting them.

AxE is primary from the first integrated pilot run. JSONL remains an existing compatibility/export fixture surface, not another live planning journal. Pilot work is excluded from legacy PM JSONL mutation and legacy tasks-app automation. An external issue may be observed or mirrored by an explicit adapter; native Telos work does not require one to exist, run or be verified.

## 6. Entities and identity

An **intake record** is a durable incoming observation. A **Telos pursuit** is an outcome-seeking graph. A **Telos node** is an objective, action, artifact, inquiry, decision, question, observable or claim. A **product commitment** means an intended result represented by a node or named graph view; it is not a mandatory separate tasks-app issue. An **execution attempt** is one bounded try at one eligible node, and a **harness task** is its local process representation. Requests, intended outcomes, attempts and evidence remain distinct even when task interfaces share canonical work identity.

Native identity is the authorized workspace/pursuit/node coordinate with the owner's immutable identifiers. UI display codes are aliases, not new work records. Optional provider bindings connect external instance/workspace/issue IDs to nodes, with provenance and a declared direction of authority. One external issue may refine into several nodes, and an objective may collect several external observations; internal actions and research require no issue. Bindings must not be inferred from matching titles.

A feature is a stable objective/node or a declared view over an identified subgraph, with facets for product area, audience, implementation and release. No new node kind or duplicated lifecycle is needed just to render frontend/product-marketing views. Specification headings link to canonical nodes and describe intended behavior. Verified implementation state comes from the graph and receipts, not manually edited completion notes in the spec. Section 15 defines this feature projection.

Telegram identity includes provider, bot/account, chat and message identity, with thread/topic context. An edit revises the same intake; revision and delivery identities differ. A callback references its decision/proposal revision and callback identity. An update ID alone is not semantic message identity. Legacy records missing chat scope remain ambiguous rather than being merged across conversations.

Edit ordering uses recorded receiver update/session order plus available source metadata. Conflicting revisions that cannot be authoritatively ordered remain explicit ambiguity, not a fabricated source-latest state. Dispatch and completion cannot rely on unresolved relevant ambiguity. An authorized resolution is its own recorded input.

Attempt identity is separate from process/session handles and survives terminal replacement. Evidence binds producer run, node, check, artifact revision and environment. Durable append position is a cursor, not event identity. IDs, hashes and schema commitments reuse owning contracts. Native UI projection rows are keyed by canonical work identity and source basis, never by a second autonomous ticket identity.

## 7. Event and command contracts

Use typed, receiver-oriented commands and existing Telos admission, not an untyped event bus. Proposed integration operations include intake capture, plan proposal, attempt request, verification submission, notification and pause/resume. Their wire names are finalized from owning declarations. They are not arbitrary fields appended to Telos's closed `telos/commit` payload. A UI or CLI uses the field-ownership map: tasks-app-owned fields may be committed to tasks-app immediately, while Telos-owned graph semantics are submitted as idempotent Telos commands and mirrored back by the sync adapter. Shared mapped fields carry revisions and explicit conflict handling; synchronization is a contract, not a background last-writer-wins repair job.

**VO is the default for new internal pilot protocols and durable domain records.** Reuse the owning schema identity, generated codecs, evolution rules and canonical records. No new ad-hoc JSON envelope, private JSONL journal or separately maintained TypeScript/Python copy of the model is introduced. Existing provider JSON contracts, such as Telegram or TypeSafe HTTP, terminate at a validated edge adapter. Debug/CLI JSON may render the canonical model; it is not a second internal authority. Existing Telos export compatibility is retained deliberately, not expanded into another machine protocol. [S9]

VO adoption is not a claim that every client path is already implemented. Qualify the concrete codec and client in P0. When a language binding is missing, implement it at the owner or invoke an existing generated client through a supported boundary; do not hand-write another codec. Versioned fixtures verify old stored data, additive fields, rejected unknown critical semantics, schema mismatch and explicit migration. A schema hash proves identity, not that a semantic change is backward compatible. Authorization is separate from serialization.

At external admission record scoped source identity/revision, authenticated principal, schema, causation and relevant basis using the owner's envelope. Validate schema, workspace, allowlist, expected revision and idempotency before creating a durable obligation. A body claiming `authorRole: human` is not authentication. Model answers, issue text, scraped content and tool output cannot grant permissions. New command families cannot bypass the existing propose/commit or human-authority boundaries.

A native create command uses a stable workspace-scoped operation identity and explicit intended node/proposal identity. For an already admitted command, an authorized receipt query returns the immutable original admission/proposal outcome without re-executing it; a different body under that identity is a visible conflict. Creating a staged proposal is not accepting it. Only the owner's authorized commit changes the graph. Duplicate UI submissions therefore cannot create two nodes or accidentally approve an agent proposal.

The tasks-app pending-command lane extends the existing integrations outbox under one delivery/recovery owner; it does not create an independent command ledger. A source webhook-delivery receipt remains a separate inbound provenance record, referenced rather than reinterpreted as an outbound command. A backward-compatible schema migration identifies domain/encoding/schema and stores new internal command bytes in the qualified generated VO representation while preserving existing legacy rows. The outbox atomically stores the command and a local DeliveryRecord, not an authoritative copy of the requested work. DeliveryRecord is mutable transport tracking (pending/sent/unknown/confirmed); it is not Telos's immutable AdmissionReceipt. On confirmation it stores the owner-issued receipt identity and a verifiable cached result. A local state update cannot manufacture, rewrite or supersede AdmissionReceipt. Its relay submits that same identity to Telos, records the admission result and updates the bound tasks-app record by canonical node/binding and source cursor. Network loss after Telos admission is reconciled by the same command identity. A synchronized Telos-owned field never implies an unobserved command was admitted. Sync update and checkpoint advancement are transactional or otherwise demonstrably replay-idempotent.

Every external effect has a durable intent and an outcome of confirmed, failed or unknown. After possible success followed by timeout, reconcile the original operation before retrying; do not assume nothing happened. At-least-once transport plus idempotent admission is not universal exactly-once execution. Deletion/correction/retraction has an explicit authoritative lifecycle; UI cache deletion never deletes a Telos node. Privacy erasure is a separate audited operation, not concealed log rewriting.

### 7.1. Transport, optional legacy adapters and unknown delivery

Relays may poll/back off for delivery, but cannot choose work, grant leases, approve completion or extend execution budgets. Business deadlines and retries are AxE state/timer decisions. A relay redelivers the same intent and does not synthesize a replacement attempt. Malformed records and partial source failures remain visible and recoverable; a failed fetch is not an empty successful project.

Optional external issue adapters remain scoped observations/mirrors. Their own database/idempotent-create/outbox contracts must be qualified before external writes, but they are not on the native pilot's critical path. Snapshot polling cannot prove a complete edit history. If an external revision supports a gate, verify the relevant current version at admission or refuse an unverified basis. Do not promote a provider's ordinary Done into Telos satisfaction.

Telegram delivery-unknown is terminal for that send attempt but visible in the delivery report and task activity. The original intent is not automatically resent or edited without a known message identity. The operator may acknowledge uncertainty or authorize a new replacement intent carrying the original report reference and a possible-duplicate notice. Confirmed work and unresolved notification delivery are different states. Failure to notify does not roll back a graph transaction.

### 7.2. Original authority, receipt lookup and concrete VO access

A pending command carries the original authenticated principal/delegation, permitted scope, validity deadline and nonce or equivalent single-use operation identity. A relay's service credential never substitutes its own authority for the author. First admission, including delayed delivery after restart, checks that original principal's **current** permissions, delegation, expiry, target basis and pause policy. An unadmitted command whose authority expired is rejected under that identity; it cannot be revived by a new privileged relay.

An already admitted operation is different: receipt retrieval does not re-admit it and cannot rewrite its historical outcome after expiry/revocation. Current read authorization is evaluated against the requester's own viewing rights, not the original actor's present role. An authorized auditor can read historical admission after the original actor is revoked; a requester without viewing rights is denied access without changing history. Returning an old admitted receipt performs no new effect and does not restore permission to execute pending work. Revocation/pause is a separate admitted policy/cancellation input enforced at dispatch and effect boundaries. A new permitted action requires a new explicitly authorized intent, never mutation of the old receipt. The lost-ack-plus-revocation test covers both admitted and never-admitted cases.

A submission-validity deadline constrains first admission, not historical receipt lookup. Execution permissions have their own bounded validity: before every delayed/new external effect the gateway rechecks the current execution authorization expiry, lease generation, incarnation, revocation and pause state. An expired execution capability blocks the effect even when its originating command was admitted earlier. None of these checks rewrites an old admission. A command with only a submission deadline is not an indefinite execution grant.

The selected Python path is a **generated VO Python binding in the AxE-owned `packages/vo` code-generation/runtime surface, wired into `packages/axe-client-python`**, with the same normalized schema artifacts and golden corpus as TypeScript/Rust. This is a named P0 implementation dependency, not an existing capability claim. The applicable family declarations remain owned by `packages/containium-wire-schema` or their already-declared domain owner; extend rather than hand-copy them. Python CLI imports remain lazy. The tasks-app writer/relay uses the qualified generated TypeScript codec for those same declared records.

P0 exits only after those exact writing/reading boundaries pass codec and stored-history conformance. P1 entry requires that completed P0 receipt, so the codec gate is both P0 exit and P1 prerequisite; P0 cannot be declared complete without it. Until then no durable native pilot commands/evaluations are written in that lane; isolated in-memory or recorded format fixtures may be inspected but do not satisfy P1. Missing Python/gateway support keeps P1 blocked on this explicit owner dependency; temporary JSON persistence is not a fallback.

## 8. AxE loop and PM decision functions

The production path is the real AxE runtime: **log → typed slots → rules/facts → decision function → admitted operations/signals → log**. PM-specific agents are domain agents for coordination and attempts, not replacement Telos planners. Use the Rust VM and native supervised runtime already represented by Containium and `targets/telos-service`. Scenario is a test harness, not the production server. A JS/Python reducer that merely imitates the loop cannot satisfy the pilot gate.

The pilot selects pure coordinator functions, deterministic over admitted state and the runtime's deterministic context, returning declared effects. This is a pilot mode choice, not a claim that AxE lacks its cooperative/non-cooperative generator modes. They do not read files, call models, query minigraf, fetch current HTTP state, invoke a terminal or read an uncontrolled clock. Research and model routing are operations with recorded requests and responses. Time passes through the runtime's timer/event semantics. Versioned policy, tie-break order, quota observations and input basis are included in the decision record so replay explains why an attempt was selected.

Telos owns dependency readiness, specification maturity, checks and the planning lease. PM policy applies execution constraints: environment availability, scope, privacy, risk, capacity, quota observations and the human's current permissions. The resolver advertised in a coordinator notification is a hint until checked against the authoritative capability registry; a node kind must never accidentally become a model or worker type. The existing brief signal is a push optimization, not an irrevocable permission to launch.

Dispatch is a protocol: read a basis-stamped eligible node; evaluate the current policy; request its planning lease; receive an authoritative grant; create the corresponding attempt durably; submit the bounded worker operation; record its acceptance. Re-check basis and permission when admission or execution has been delayed. A stale brief, revoked permission, closed pursuit or changed specification causes reconciliation, not execution from an old queue snapshot. No polling process independently grants work beside Telos.

Every attempt has a lease generation/fencing value and a bounded expiry. A late worker may submit historical output, but an obsolete generation cannot complete the current attempt, release another worker's lease or update current canonical work. Heartbeats renew only the held generation. Restart adopts a confirmed live attempt or expires it through recorded policy; it never starts a second worker just because a local PID disappeared. If the upstream lease contract lacks the required atomic admission/fencing capability, implement it at that owner and prove it before enabling parallel workers.

A stop signal prevents new dispatch immediately at admission and blocks new external writes at the executor boundary. Already issued effects may still finish; they are reconciled and reported, not described as rolled back. Cancellation distinguishes requested, acknowledged and terminal. Resume is explicit and revalidates the current plan, credentials and quota. Use runtime scheduling rather than a parallel timer daemon; operating-system process supervision remains infrastructure, not a business scheduler.

### 8.1. Concrete PM artifact and crash-safe dispatch

The proposed AxE package is `axe.sc/axe:packages/rig-pm`, built and tested through that repository's Nx/Containium machinery; the package name is new, not an existing artifact claim. It owns PM execution-policy and attempt domain agents. `pm-cli:pmlib/adapters/axe.py` is the proposed lazy client boundary; the Python CLI remains a commander/renderer and does not gain a local scheduler. Pilot mode has an isolated PM_HOME, does not install legacy reminders cron, and rejects legacy `record`, `ingest`, `timer` and `reconcile` mutations for pilot-bound scope. The refusal points to the AxE-backed command. Read-only legacy history is still available.

Cross-agent admission is an idempotent saga, not a fictitious distributed transaction or synchronous RPC inside decide. First persist a dispatch intent with a stable attempt identity and its target basis, then emit a lease request. A later lease-grant event carries the authoritative Telos generation and that same intent identity. The attempt admission records this grant before emitting worker submission. A subsequent worker-accepted event records the actual process handle. Each step is a separate synchronous decision cycle with a durable outgoing effect; no function waits for another agent inline.

Telos serializes competing claims per node with atomic compare-and-set semantics and returns the same grant for the same claim identity. Its lease generation is the attempt fence; PM never invents a competing lease generation. An intent whose grant response was lost retries/reconciles the same claim. A grant with no admitted attempt is visible as an orphan and releases/expires via the owner's logged protocol; no worker starts for it. A worker submission repeats only the same attempt key, which the supervisor deduplicates durably. Renewals apply only to the authoritative held generation. Race, orphan and lost-ack tests are blocking P0/P3 gates, not deferred until a second worker is deliberately enabled.

The supervisor checks the current environment incarnation, held generation and execution permission for each external effect. Lease expiry/revocation removes write permission just like pause; it is not merely a rule against late completion. Workers have no direct tasks-app, Telegram or repository-push credentials, so they cannot bypass the gateway using an otherwise valid personal token. A long-running already issued effect remains reconcilable and is not falsely described as canceled.

An independent operator stop latch lives at the authorized supervisor/gateway boundary and works even when the AxE host, disk or log is unavailable. It closes egress/write admission, revokes attempt credentials and may terminate only the registered pilot process group/container. A fail-closed watchdog suspends effect admission when coordinator authority cannot be refreshed. When AxE recovers, it receives the stop receipt before dispatch can resume. This safety interlock cannot grant work, extend a lease or choose a replacement, so it is not a second business scheduler.

### 8.2. Optional semantic evaluation — Laya default, Jev secondary

The semantic-evaluation layer is not the Telos planner, AxE runtime or permission authority. **Laya is the default semantic judge for current pilot work; Jev remains an optional secondary/fallback/comparison backend.** Use the layer first for shadow-only intake classification and matching a request against an authorized candidate set. The model's Choice/Noul/Score answer is data; an AxE Decision is a typed effect/wait instruction; a Telos decision node is a planning divergence. None is interchangeable with a passed check or human approval. The [research and integration contract](https://spec-dev.hyperide.ai/spec/rig-axe-typesafe-decision-research) records the reviewed API/SDK and current limits. [T1]

For the selected pure coordinator, emit a declared evaluation Op; execute its hosted SDK/HTTP call in the Op's I/O realm; validate and record the response; reduce the result into typed state; then apply deterministic policy to emit permitted effects. Never call `client.systemOne` or raw fetch inside a hermetic pure decide function. AxE also has generator modes: yielding the same Op is a valid alternative subject to that mode's replay/retention contract, not permission for unmanaged async I/O.

Pin input/specification basis, candidate set, question/rubric version and model identity. Bind provider attempts to a bounded budget and scoped evaluation identity. Validate response answer IDs, types, candidate membership and numeric/distribution invariants; the SDK's TypeScript return type alone is not runtime proof. Persist the accepted internal result through generated VO-backed contracts, with provenance and the policy version that consumes it. Provider JSON remains only at the external boundary.

Model probability/confidence is not Telos evidence confidence. Preserve the difference between probability of a proposition and certainty about an answer; include none/uncertain/defer paths. A semantic match cannot erase a requirement, admit a structural graph change, grant a lease, accept risk, close a pursuit or publish a release. Authorization and current basis are checked independently even when the model is confident. Do not use Laya, Jev or any other semantic judge for graph priority, counts, exact dates or proof that a test passed.

Known admitted evaluations are reused on replay; a provider timeout before durable response admission may still incur another inference/cost, so do not promise exactly-once remote computation. AxE owns retries and budgets; disable overlapping SDK retry loops, bound timeout/cancellation and suppress secret-bearing body logs. A stale response is historical only. Outage or quota exhaustion yields visible abstention or a previously authorized fallback, never a privacy downgrade.

No provider key or private source data is needed for P0–P3. Hosted evaluation requires explicit scope and verified account privacy/retention terms. Qualification uses English/Russian/mixed fixtures, adversarial inputs, calibrated error-versus-abstention, latency/cost observations, and real-runtime crash/duplicate tests. The semantic-judge layer remains optional; Laya being the default route does not give it authority, and source inspection is not a live model benchmark or production endorsement.

### 8.3. Interleaved semantic pre-review between coding steps

The model-assisted experiment includes a second, distinct shadow workload: **semantic pre-review of a stable patch between bounded coding-model steps**. Intake classification/matching remains the cheapest adapter fixture; it is not the entire evaluation scope. Pre-review aims to detect a narrowly described problem early enough to improve the next step, not certify correctness, replace independent final review, or make every intermediate token a gate.

Trigger from an admitted artifact checkpoint or completed bounded tool step, not every token, keystroke or fixed-rate timer. Applicable deterministic parsing, type/lint checks and exact diff scoping run first. Assemble the smallest authorized context: changed code, relevant specification clauses, interfaces/dependencies and existing check observations. Record omitted/missing context. Code comments instructing the evaluator to approve itself remain untrusted data.

Reuse the evaluation Op and generated VO-backed receipt from section 8.2. Pin pursuit/node, producing attempt, artifact revision, relevant specification basis, context/candidate scope, question/rubric version and model. Bounded questions may ask whether the patch appears to omit a stated error path, changes an unrelated authority boundary, duplicates a named source of truth, or warrants deeper review. Include unknown/not-applicable. These are semantic assessments, not proof that code executes correctly. Batched answers do not establish statistical independence or a sound joint probability.

Policy consumes only a response matching the current requested basis and permitted scope. Coalesce equivalent pending work and bound concurrency, elapsed time, input volume and call/fix budgets. Newer patches supersede pending evaluations without rewriting their eventual historical receipts. Stale results cannot approve, block or revise newer artifacts. Timeout means evaluator-unavailable/abstain, not clean. Continuing during outage is a versioned policy choice; mandatory compiler, security, review and release gates stay enforced. Shadow observations never mutate the plan or worker instructions.

After qualification, explicitly enabled advisory mode may deliver one deduplicated, basis-bound finding to the next coding step through the existing attempt/feedback channel. Stable feedback identity survives receipt replay without restarting a worker twice or resetting its fix budget. Group correlated warnings instead of sending repeated human questions. Value/authority choices retain the human-decision protocol. The evaluator cannot approve, commit, accept risk, grant leases, merge, deploy or close pursuits. Final verification remains independent and artifact-bound even when pre-review finds no problem.

Measure time to a useful finding, per-defect precision/recall, false interruptions, misses caught by final review, extra coding calls, abstention and full-path cost/latency including discarded responses. Website fixtures never count as inference or real AxE evidence. Vendor game demos and writing experiments motivate this workload but supply neither its latency SLO nor code-review accuracy. See [the interleaved-review research](./rig-axe-interleaved-review-research.md).

### 8.4. Local evaluator baseline and the training boundary

Compare a local bounded-output evaluator before commissioning training. The inspected `harshatheg/Qwen-2.5-1B-RLCD` model card describes an inference implementation using Qwen2.5-1.5B-Instruct with parallel constrained decoding, not evidence of new RLCD weights or Jev equivalence. Distinguish valid syntax, semantic accuracy, empirical calibration and action authority. Programmatic enum/JSON assembly establishes only syntax; normalized softmax alone establishes neither accuracy nor calibration; no model score establishes authority.

Qualify source commit, base weights, license, primitive semantics, multi-token candidate handling and resources. Author-reported MLX/M4 Max numbers are not pilot results. A separately approved Mac/MLX benchmark is an external experiment and cannot replace the all-in Linux/ARM64 topology. Any Linux/PyTorch route needs its own packaging/workload tests. The core pilot remains provider optional.

Fine-tuning is separately authorized research, not default startup. Require an unchanged baseline, explicit data rights and bounded budget. Preserve expert-reviewed labels; incumbent model answers are not ground truth. Split by originating repository/request/thread and time where applicable, keeping revisions and near-duplicate patches together. Tune thresholds/calibration on development data, freeze them, and evaluate held-out English/Russian/mixed cases. Report per-defect/per-language errors, abstention/coverage, calibration and full-path latency/resources. Promotion uses the fixed evaluation plus unchanged non-model gates.

Access to Laya, Jev or another semantic-judge provider is not by itself a credential/data grant to the pilot or permission to transmit private source. Inspection, interface fixtures and the website require no provider account. Connected inference still requires operator model/data authorization and verified account terms.

## 9. Telos lifecycle, evidence and human decisions

Preserve Telos's two axes: specification maturity and realization. A request classified as actionable is not necessarily finalized work. Research, alternatives and unanswered questions can block the action. The pilot dispatches only the owner-reported frontier under the required specification/check/leaf conditions; it does not recompute those predicates from issue statuses. A repeated research result should enrich the same evidence trail, not create another unconnected task because the session changed.

Preserve the distinction between `satisfied`, confidence-gated `done`, a released result and a pursuit explicitly closed as `achieved`, `discarded` or `stuck`. A merged PR is an external observation. A worker exiting successfully is an execution fact. Neither proves a deployed behavior or closes a pursuit. Source claims about satisfaction and evidence freshness are tested against the actual runtime predicates; the adapter must not silently redefine those predicates to match a five-state ticket view.

A verification receipt identifies the exact repository and commit/tree, artifact digest where applicable, check definition/version, environment or deployment identity, producer, outcome, provenance, time and relevant input dependencies. Evidence references are immutable or version-pinned and access controlled. Large stdout/stderr, screenshots and traces stay in artifact storage. A passing command without an authoritative target revision does not support automatic completion. An unavailable evidence object is shown as unavailable, not silently counted as passing.

Evidence freshness is evaluated against the current requested result. Changed relevant inputs trigger invalidation or a required recheck according to the check contract, not merely a timer. A standing `claim` remains distinct from the historical action that first established it. Regression does not rewrite the earlier successful attempt, erase historical receipts, or reopen a terminal pursuit by changing its root status. Open a linked corrective pursuit/action when the lifecycle requires it, and preserve the original completion's meaning.

Human decisions and approvals remain distinct. Researchable uncertainty produces inquiry work first; only unresolved value/authority choices reach the human with alternatives, consequences and evidence. Structural plan changes use Telos's propose/commit authority. Evidence and answers use their direct-plane operations. A dedicated pilot policy may delegate a narrow class of mechanical actions, but the agent cannot self-approve a plan change or fabricate a human `pursuitClose`. Independent countersign rules are inherited only where the owning policy and tests support them.

Approval requests bind the authenticated actor, workspace, concrete action, target revision, scope, expiry and nonce. Telegram button clicks are verified callback inputs, not shell snippets or free-form permission strings. Repeated clicks are idempotent; old buttons are visibly stale after a revised proposal. One human answer resolves the linked Telos question, tasks-app representation and Telegram thread through a shared command path. A comment containing the word "approved" is not an authorization event.

## 10. Product commitments: task-cli and tasks-app

tasks-app and task-cli are **independent OSS tools first, integrated peers when RIG/Telos sync is enabled**. tasks-app keeps normal writable issues/projects in PostgreSQL whether or not Telos exists. Integration binds selected tasks to Telos nodes instead of requiring every Telos node to become a task. `task-cli` routes a requested operation to the appropriate standalone backend or integrated sync path. Rich Telos graph semantics are not flattened into five ticket states; unsupported mappings stay explicit.

Useful task-cli discipline is preserved in the common model rather than implemented as competing rules. Motivation maps to `prose/why`; implementation description to `prose/how`, `pseudo` or `op`; specification links to existing references; acceptance to checks and their evidence. Screenshots are referenced artifacts for visual checks, not mandatory evidence for every runtime assertion. User impact, cost of inaction, assignment and additional facets require an explicit gap analysis before adding fields.

The attribute model is extensible, but the inspected implementation does not accept arbitrary new control attributes: cardinalities and control vocabulary are validated; open descriptive namespaces do not automatically affect readiness or satisfaction. Reuse existing attributes first. When a new semantic attribute/check is needed, add it with its owner, cardinality, validation, VO declaration and derived behavior/tests. A schema addition is not an excuse for a second private bag of task metadata. [S8]

The tasks-app integrated mode uses its existing shell, authentication, navigation, PostgreSQL domain model and reusable UI components. For a Telos-bound task, editable controls update tasks-app-owned fields locally and submit Telos-owned/mapped changes through the scoped sync gateway with a stable operation identity. Show local-only, syncing, synchronized, rejected, conflicted, disconnected and stale states separately. Structural Telos edits follow propose/commit authorization, while evidence and answers follow their owning direct-plane operations. No optimistic Telos Done or hidden automatic approval is permitted.

The existing tasks-app PostgreSQL is a permanent product database, not a disposable projection store. It holds standalone records, synchronized task state, binding metadata, sync cursors/receipts, local UI/product fields, imports and integrations. It is not replaced by minigraf or Telos. The sync adapter is optional and removable. Routes resolve binding/origin server-side; neither a forged integration flag nor a generic PATCH may bypass Telos admission for Telos-owned semantics.

Tracked implementation for the optional synchronization contract: **HYP-1514**.

### 10.1. Completion and synchronization admission

A completion request is a request to verify the target under the current basis, not a write of `status=done`. Canonical satisfaction/done and pursuit closure come only from Telos's checks, confidence and authorized lifecycle. Every task-cli/UI/bulk/integration path targeting a pilot entity must use that admission; direct projection-status writes are refused. The P2 route inventory includes the existing issue PATCH and bulk routes, task-cli entry points, integration handlers including merge-to-Done, and internal jobs; every route must prove origin-scoped refusal before native views are editable. Existing merge-to-Done automation has no authority over native Telos views. Explicit human accept-risk remains a distinct privileged operation, never a disguised passed test.

Verification admission carries the canonical node, check identity/version, relevant artifact and plan basis, producing attempt, current environment incarnation and authoritative lease generation. The owner rejects obsolete or duplicate outcomes under the same semantic identity. Workers cannot directly mutate tasks-app, Telos or Telegram with a broadly privileged token. A late result may be retained historically without becoming current evidence.

After canonical Telos admission, tasks-app receives a basis-stamped synchronization update and records the Telos binding/cursor atomically with its local update. Unknown delivery is retried by operation/source identity, not by repeating verification or holding a work lease indefinitely. Either product can be temporarily offline; the adapter catches up from durable receipts/events. Reconciliation follows the declared field-ownership map: Telos-owned semantics are restored from Telos, tasks-app-owned fields remain local, and genuinely concurrent edits to shared/mapped fields are surfaced for deterministic or human resolution rather than silently discarded.

If a later explicitly selected external tracker is authoritative for some non-native work, its completion/idempotency/automation guard is a separate provider contract. It does not alter the native pilot's one-source rule or require a two-phase finalization across Telos and a redundant tasks-app issue.

## 11. Communication, ideas and specification feedback

The Telegram adapter uses the existing tg-cli/tg-ctl transport. The pilot does not call `getUpdates` beside the live receiver, change the bot's webhook, redirect all account messages, or consume unrelated private conversations. Fixture mode has no Telegram credentials. Connected mode is constrained to a separately approved bot/chat/topic and verified sender allowlist. A receiver that cannot expose stable scoped identities is extended before connected mode; its ambiguous legacy output is not patched by guessing.

Classify each intake as information/question, idea, actionable request, correction, decision response or permission response. Capture it durably before classification. A request for an explanation can be answered without opening an implementation ticket. An idea remains an idea until triaged. Link revisions and follow-ups to their parent requirement where justified, with an undoable human-visible decision when deduplication is uncertain. Source text and attachments remain attributable; automated summary is an additional representation, not destructive replacement.

Reports distinguish received, planned, awaiting decision, running, verifying, blocked, failed, completed and delivery-unknown in ordinary language. Include the current result, missing evidence and one actionable next step, with links into tasks-app/Telos/spec-web. Emit on meaningful transitions and aggregate repetitive heartbeats or retries. A human must not receive a flood of mechanical approvals. Notifications are projections; failure to notify does not roll back a confirmed workflow transaction.

Submitted spec-web comments become durable scoped intake references `(spec identity, comment ID, submission/revision)`. Questions request an answer; remarks propose a change; neither authorizes code execution or approves a plan by itself. A reply is written through the existing spec-web reply operation and retains the original thread. Pending drafts are not imported as committed requests. The pilot adds a recoverable event/cursor adapter rather than relying solely on best-effort injection into a tmux pane.

## 12. minigraf, Containium, VO, IAM and Hero

**minigraf is an embedded graph database, not a network database service.** The pilot uses the actual Forgejo fork for authorized relationship and historical queries: why a node exists, which commitments it supports, what it unblocks, and what evidence was known at a given point. It does not replace tasks-app's issue database, AxE's authoritative log or the VM's derived facts. Prefer the existing AxE integration boundary and supported binding; verify the installed binding matches the fork and pinned source rather than assuming a similarly named published package is equivalent. [S2, S7]

The graph projector ingests committed source transactions with stable identity, stores its cursor atomically with the corresponding graph updates, and exposes its basis with every response. Crash between fact application and cursor advancement must be harmless to replay; if that atomic capability is absent, implement idempotent batch application and verify it explicitly. Graph files are opened by their owning process, not shared writable among arbitrary containers. Index formats, sidecars and schema versions belong to the selected engine; checkpoint/backup includes its complete supported durable set, not just a file chosen because the README calls the engine single-file.

Datalog queries are bounded by time, memory and result size. Authorization is applied before facts enter a workspace projection and again at the query boundary. Broad graph queries cannot cross a workspace or private-source boundary. Retrieval lag is shown, not hidden; correctness-critical dispatch never depends on an eventually consistent graph answer. Dropping the graph projection and rebuilding at the same admitted basis must reproduce the normalized answers used by the pilot. Historical knowledge time and source-valid time are preserved rather than collapsed to a single updated-at timestamp.

**Containium** supervises the native runtime and generated agent artifacts inside the controlled environment; **Docker-compatible containers** are the outer packaging/isolation mechanism. They are different layers. Reuse the inspected `telos-service` Containium generation/readiness/development targets instead of inventing a second agent loader. The final image entrypoint is a P0 compatibility deliverable: source target existence alone does not establish a Linux/ARM64 packaged build. Code generation has one owner per output, explicit build dependencies and no copied stale generated directory. [S1]

**VO** is the default internal protocol and durable-record discipline for new pilot integration, including schema identity, evolution and generated codecs, not merely an optional IPC optimization. Third-party JSON terminates at the edge and debug/CLI renderings remain derived. The observed wire specification explicitly lists remaining client/Python/gateway gaps; P0 must qualify the exact machine path or implement the missing owner capability rather than adding ad-hoc JSON. Old/new schema and stored-history fixtures prove the supported evolution. No universal backward-compatibility, zero-copy or performance result is inferred from a format name. [S9]

**IAM** is evaluated through the existing IAM event/policy packages, but full organization-wide identity migration is not a prerequisite for a local fixture. The mandatory security contract is authenticated service identities, least privilege, workspace scoping and explicit human authority. Connected mode cannot proceed until the chosen identity boundary proves these properties. tasks-app authentication remains authoritative for its API; a pilot credential does not become a tasks-app owner token. Lack of a ready IAM integration never means unauthenticated network exposure. [S1, S5]

**Hero** is an optional inquiry resolver for one allowlisted research source after the core deterministic path passes. It returns cited observations with provenance and timestamps, not executable instructions. Network policy blocks private/metadata addresses, unapproved redirects and unbounded downloads; browser profiles and cookies are isolated. Scraped content cannot grant permissions, select secrets, change the executor policy or request arbitrary shell commands. No general autonomous browsing is needed to prove the initial workflow. [S1]

## 13. Container-first environment

The deployment unit is a named disposable pilot environment with a pinned manifest of repository commits, dependency locks, generated-artifact versions, platform and image digests. Sources remain in their current Forgejo repositories during the pilot. A manifest composition tests the future monorepo seam without relocating ownership or rewriting histories. A clean installation outside the developer's existing workspaces is mandatory: no accidental dependence on `~/xp`, a sibling checkout, a developer PATH, global Python packages or untracked build outputs.

The local topology comprises a native AxE/Containium host carrying Telos and PM agents; a scoped integration gateway; an isolated tasks-app web/API plus PostgreSQL; an artifact volume/service; and a bounded worker executor. Telos review UI is a client of the pilot's AxE-primary store. The minigraf projection may be embedded in the query owner rather than deployed as another service. Fixture sources and notification sinks replace external services initially. A connected Telegram adapter and optional Hero resolver are separate explicitly enabled capabilities, not default startup dependencies.

All durable mounts have one owner: AxE log/state, tasks-app database, evidence artifacts and rebuildable graph indexes. Source checkout used for compilation is separate from the disposable repository edited by workers. A worker receives only its bounded workspace and necessary inputs; it never receives the user's home, SSH directory, credential store, real `.telos`, PM journal or production database. The control plane and workers do not receive a Docker socket. Lifecycle operations are performed by the authorized outer operator, not an agent with container-host privileges.

Prefer unprivileged users, a read-only root filesystem, dropped Linux capabilities, no-new-privileges, bounded temporary storage, CPU/memory/PID limits, explicit health/readiness checks and loopback-only published ports. Verify actual enforcement on the selected runtime, not merely YAML acceptance. Dependency readiness is checked separately from process startup. Compose secrets are granted only to services needing them; local secret files still require protected host storage and are not claimed to be an encrypted secret manager. Docker's rootless mode can reduce daemon privilege, but does not replace the worker threat model. [D1, D2, D3]

Network separation is enforced, not implied by a network name. Fixture mode denies external egress. Connected mode uses a gateway/firewall allowlist for the approved providers; a plain Compose bridge is not an outbound domain filter. Secrets are injected per service or per attempt, redacted from logs and never baked into images. Provider revocation and unauthorized destination tests are release gates. A local-model route and a hosted-model route are separate capabilities; neither borrows another account or falls back across a privacy boundary.

Initial experiment budgets are deliberately small: one active coding attempt, one independent verification attempt, at most two worker processes concurrently, and no automatic merge/deploy. A run may make at most ten model invocations and three fix iterations, with a thirty-minute overall attempt deadline; these are **proposed guardrail defaults**, not measured capacity or a promised completion time. Before launch, the operator records explicit CPU, RAM, disk and log-volume limits supported by the actual machine. Budget exhaustion produces a paused/blocked outcome and a receipt, not an unlimited retry loop.

The environment's `doctor` must prove the real container backend, image platform, native runtime availability, enforced filesystem/network/process limits, secret isolation, available disk, writable durable mounts and stable restart behavior. A command called `docker`, a successful version string, or simulated Compose output is insufficient. Missing isolation stops connected execution; it must not be worked around by mounting the host home, granting privileged mode or borrowing the production bot's configuration. The source audit records current readiness rather than treating this machine as prequalified.

### 13.1. Selected platform and build graph

The P0 target is **all-in Linux/ARM64 in an isolated container VM**: the native Containium/AxE control host, Telos/PM agents, integrations, tasks-app/PostgreSQL and workers run inside that environment. The Mac is the operator/browser, not an undocumented host-side AxE dependency. This is a selected packaging target, not a claim that the current Mocker command implements it. If the native dependencies cannot build or isolation cannot be enforced, P0 fails and the owner must fix packaging or propose a reviewed topology revision; it may not silently fall back to JSONL, host execution or a JS VM substitute.

The reproducible build graph is: resolve approved Forgejo commits and lockfiles into a clean build context; build the owning VO/Containium code-generation targets; build the real Rust VM and native artifacts; build Telos and the proposed rig-pm package; generate the telos-service/PM Containium declarations and client artifacts; verify schema IDs and artifact digests; assemble platform-specific pinned images; run native readiness, real-VM integration and isolation probes. Dependency/output edges stay in the owning Nx targets and image build manifest. Images consume declared immutable build artifacts, not arbitrary copies from a developer's generated directory. The query projection is owned by the pilot gateway process; tasks-app calls that gateway and never opens its minigraf files.

## 14. Threat model and permissions

Separate principals for operator, planner, execution coordinator, worker, verifier and notification sender. The operator can enable a mode; a planner proposes structure; the coordinator requests bounded attempts; a worker writes only its disposable checkout; a verifier emits evidence for checks it actually ran; the sender delivers only permitted notifications. Product administration, pursuit closure, accepting risk, repository merge and deployment remain explicit capabilities. A worker's textual report is never sufficient authority for another role's action.

Workers are treated as potentially compromised by task content, dependencies, compiler output or retrieved pages. Rootless containers alone do not establish a strong sandbox for hostile native code. The pilot therefore combines no production secrets, restrictive mounts/egress, a disposable host/VM where needed and tested isolation. Expanding to untrusted public repositories requires a separate sandbox review. No broad credential forwarding, host SSH agent, host Docker socket, shared browser profile or unrestricted package-install network is permitted by default.

Source retention is minimized. Keep request provenance and necessary excerpts rather than bundling complete private repositories or chat history for model providers. Prompt assembly applies source permissions and a byte/token budget before dispatch. Reports and screenshots are checked for secrets and personal data. Raw logs have explicit retention and access rules; redacted human summaries are separate from protected debugging artifacts. A content hash is provenance, not an authorization check.

## 15. Human surfaces: CLI, GUI and review workflow

`pm` is the operational entry point for diagnosis, execution policy, attempts, pause/resume, recovery and reporting. It explains the Telos frontier and why an otherwise ready node cannot currently run. It does not manage another backlog or recompute dependency priority. New command names/flags are generated from their actual parser during implementation; the current Release-1 CLI is not claimed to dispatch. Legacy read-only views stay available in their own scope.

`task` supplies familiar creation, reading, inspection and verification commands over canonical Telos work. `telos frontier`, `telos agent brief`, `telos propose`, `telos commit` and `telos serve` are existing seams to qualify, not APIs to duplicate. Read/command adapters reuse declared client schemas. Spec-web remains prose review plus anchored feedback, not an independent plan lifecycle. No additional TUI is needed before shared query/command contracts are correct.

In tasks-app, native task details show their canonical node, objective, specification maturity, computed readiness, priority explanation, direct/transitive unlocks, active attempt, checks, evidence and basis. A manual boost is an explicit authorized graph input with a reason; drag order or a stale cached list cannot silently override scheduling. Changes to a relevant requirement/version invalidate only the corresponding basis. Synchronization, no eligible work and execution failure are visibly different states.

### 15.1. Feature overview for backend, frontend and product marketing

Build one **feature/capability catalog projected from Telos**. Each row references an objective/node or named subgraph and the linked specification heading; it does not create a second feature truth. Facets organize backend behavior, frontend flows and market-facing capabilities, using governed attributes or a versioned view definition. Default display order is a product-view concern; computed work priority remains the Telos frontier. A feature can link several implementation/check nodes without copying their statuses.

Show distinct dimensions: intended/planned scope; specification maturity; implementation verified against a named artifact; released availability by environment/version; current evidence freshness; and documentation/marketing readiness where explicitly checked. A planned feature is not necessarily ready for an agent, a verified implementation is not necessarily released, and approved copy is not evidence that behavior exists. Missing or stale evidence is visible. Publishing marketing material or releasing a feature requires its own authorized action.

A detail view answers what the feature promises, where it is specified, what is already proven, what still blocks it, what changed since the last proof and where it is actually available. It links both directions to specification headings, nodes, code locations, tests and release receipts. Any coverage ratio states its denominator and weighting; do not convert arbitrary node counts into a fictitious completion percentage. The initial pilot has one frontend-facing feature and one internal/backend capability to prove the view works for both.

Specifications remain present-tense intended truth, not hand-maintained implementation-status diaries. Their heading-to-node links let the UI render live state beside the prose without rewriting the normative text. Presentation preferences and unsent drafts are browser/UI state, not domain events. Submitted changes become durable commands with explicit outcomes.

The feature-level `verified` value is computed once by a versioned Telos-side projection contract, never independently by each UI. Its declared required scope identifies the feature root, required realization/check nodes, relevant artifact/environment basis and any explicitly nongating nodes. `verified=true` requires the root and every required realization node to meet the owner's `done` predicate and the required verification evidence to match the current declared basis. An empty/undeclared verification scope or unknown evidence produces `unknown`, not vacuous success; unmet requirements produce `false`. This additional view predicate does not redefine Telos's core `satisfied` or `done`. Changes to scope require the owning authorized graph/view revision. CLI and GUI consume the same basis-stamped result, with conformance cases for all/partial/unknown/stale membership.

Released availability is owned by authenticated deployer observations admitted through the proposed `packages/rig-pm` release-observation verifier into Telos observable/check records, scoped to environment, version and availability condition. This verifier validates provider receipts; it is not a deployer and cannot create availability from a merge event. The initial pilot provisions no real deploy/release writer. Its live feature rows therefore show `not recorded`; a synthetic receipt may exercise rendering only when clearly labeled fixture evidence and excluded from claims of real availability. It cannot turn verified implementation into a release by inference.

### 15.2. Interaction quality

Live updates preserve drafts, selection, focus and scroll. Concurrent submissions use expected canonical revision and expose a resolvable conflict; no blind overwrite. Escape closes the topmost dismissible layer and restores focus, menus/dialogs support keyboard operation, and command shortcuts do not collide with text editing. Narrow screens wrap content and show clear pending/stale states. Private links re-check authorization. Test these pilot surfaces without claiming every unrelated tasks-app UX defect is fixed.

### 15.3. Bilingual learning and research website

Provide an English/Russian explanation site inspired by state-lib's teaching pattern: an outcome-oriented overview, interactive examples beside explanations, inspectable architecture and clear intended-versus-tested labels. English remains the canonical normative specification. Russian explains the same concepts and links back to that contract, not an independently editable second specification. Switching locale preserves page and meaningful anchor.

Explain the actual integrated model: Telos graph semantics and computed frontier, PM execution policy on AxE, independently usable task-cli/tasks-app plus their optional synchronization contract, scoped intake, attempts and evidence. Interactive diagram nodes expose useful ownership/contract details. Research separates source claims from local measurements; benchmark ratios and model confidence never become product promises.

The workflow/pre-review laboratory is a deterministic browser fixture, labeled at its controls and results. It supports patch changes during pending evaluation, stale-result rejection, warning/unknown responses, replay deduplication and independent final verification. It sends no data to models, contains no credentials and mutates no real Telos/Tasks records. A future live backend requires separate connection and visible identity; simulated state must never appear as production telemetry.

Pre-render readable locale pages; retain semantic headings/deep links; make navigation/search keyboard-operable with Escape and focus restoration; support reduced motion and narrow viewports. Preserve local example state within the browsing session. Every control works or explains why it is disabled. Test both locales, locale/page/anchor switching, history, keyboard handling, console errors and horizontal overflow. The English master and its source digest are build inputs, not an independently maintained site copy.

Publish only reviewed explanations and approved public references. Never bundle private Slack transcripts, account details, gift/access tokens, provider credentials, unredacted logs or third-party article PDFs. Local preview is not production deployment. Public hosting or live inference is a separately authorized action.

## 16. The first vertical slice

Use a tiny disposable repository containing a deterministic, deliberately failing behavior and its independent expected-result test. A synthetic Telegram-format request asks to fix it. The adapter records one intake, Telos receives one bounded pursuit proposal and linked action/checks, tasks-app renders that canonical work without creating an independent issue, and a human or pre-approved fixture policy accepts the exact plan. PM policy requests one lease and one worker attempt. The worker edits only that repository; review and QA run independently against the resulting artifact.

The first attempt is deliberately followed by a failed verification to exercise the fix path, not only the happy path. A later verified artifact satisfies the node; canonical verification synchronizes the bound task/feature state and produces one confirmed report. Nothing is merged or deployed. Duplicate messages, duplicate callbacks, lost acknowledgments, worker restart and a stale result are injected into the same scenario. Every integrated surface must resolve to the same binding, canonical node and current attempt rather than creating parallel work. Disconnect/reconnect the sync adapter to prove queued changes and field ownership reconcile without deleting or rebuilding the independent tasks-app database.

The first slice also renders one frontend-facing feature and one internal capability with linked spec headings, planned/verified/released dimensions and explicit evidence. A second slice deliberately changes the relevant artifact after a passing check. The UI must show that the old receipt no longer justifies current completion, and a corrective action or re-verification becomes visible under Telos's actual lifecycle rules. A third slice uses one real but isolated maintenance request only after the synthetic and restart gates pass. These slices make success falsifiable before testing broad autonomous project management.

## 17. Verification matrix

Tests use real owning libraries and the real AxE VM wherever correctness depends on their semantics. Pure adapter tests remain useful but cannot substitute for cross-process replay, a PostgreSQL transaction, enforced container isolation or a real Telegram transport test. New behavior follows red test → implementation → independent review → fix → repeat. AxE tests run through the declared Nx targets, including their generated artifacts and traces; PM's existing Python tests remain hermetic.

| Case | Required result |
|---|---|
| Same Telegram delivery repeated 100 times | One semantic intake and one binding; retry receipts remain distinguishable from new revisions. |
| Same message ID in two chats/bots | Two distinct intakes, correct sender/thread context, no accidental merge. |
| Edited message, colliding revision timestamps and out-of-order delivery | Stable identity and retained history; ordered receiver basis or explicit ambiguity, never a fabricated authoritative latest. |
| Same ticket display code in two workspaces | Distinct immutable product identities and access boundaries. |
| UI pending-command receipt commits before relay crash | Same command ID is reconciled with Telos; pending data is not falsely displayed as an admitted node. |
| AxE append succeeds but response is lost | Retry with the same identity reconciles the accepted transaction. |
| Provider effect succeeds but confirmation is lost | Outcome remains unknown until authoritative reconciliation; no blind repeat. |
| Task changed after first PM ingest | Reconciliation sees the actual new revision instead of preserving an obsolete initial state. |
| One project fetch fails or paginates | Incomplete coverage is visible; a successful empty list is not fabricated. |
| Two dispatchers race for one eligible node | One authoritative lease/attempt; loser reconciles rather than executes. |
| Worker restarts; PID is reused | Attempt identity remains stable; unrelated processes are not adopted or terminated. |
| Lease expires and old worker returns | Historical output may be retained; obsolete generation cannot complete new work. |
| Pause/revoke between scheduling and execution | New effects are refused at the execution boundary; in-flight outcomes are reconciled. |
| Artifact changes after review or QA | Prior receipt does not support current completion; no merge-equals-Done shortcut. |
| Untrusted text claims human approval | No privilege change; only authenticated typed approval is admissible. |
| Old approval button after proposal revision | Visible stale-response result; no approval of a different plan. |
| Unauthorized workspace/source or leaked token attempt | Refusal before data/effect access, safe audit without exposing the secret. |
| Drop/rebuild graph projection | Same normalized authorized query results at the same source basis. |
| Cursor rollback, projection crash or graph sidecar loss | Safe replay/rebuild or explicit unrecoverable state; no silent freshness claim. |
| Worker attempts host/socket/private-network access | Denied by actual isolation, not merely a policy string. |
| Close a pursuit then receive later evidence | Historical closure preserved; no accidental reopen or new dispatch. |
| Draft edit while SSE/projection updates arrive | Draft, focus and selection survive; submitted conflict is explicit. |
| spec-web feedback redelivered | One linked review intake, one answer trail, no repeated human question. |

Additional architecture-review gates are mandatory, not optional extensions to the happy path:

| Failure boundary | Required result |
|---|---|
| Native creation admission succeeds but response is lost | Same command/proposal/node identity is returned; no duplicate node and no automatic commit. |
| Human/UI/bulk PATCH/internal integration writes Done to a native projection | Refused or routed to Telos verification; no local Done field becomes planning truth. |
| Legacy merge-to-Done automation encounters a native Telos view | Origin-scoped route refuses the mutation; only a canonical admitted verdict updates the view. |
| Lease granted before coordinator crash, attempt not yet admitted | Same durable intent reconciles the existing grant; visible orphan recovery; no duplicate or unfenced worker. |
| External effect arrives after lease expiry/revocation | Supervisor rejects obsolete generation, not only its final completion report. |
| AxE unavailable or disk full during operator stop | Independent supervisor latch closes effect admission and stops only registered pilot workers. |
| Concurrent outbox relays and timer wakeups | Duplicate delivery is harmless; relays cannot grant work or extend budgets. |
| Private/local-only work loses its permitted executor | Blocked; no fallback to a hosted provider or a more privileged principal. |
| Optional external source hides an edit or lacks a reliable version | That source cannot support a current completion gate until its basis is qualified; native Telos history is not replaced by polling. |
| Checkpoint attempted during unsettled cross-store writes | Barrier prevents sealing until classified/stable; per-store bases and unknown effects retained. |
| Restore while a pre-restore worker is still alive | New environment incarnation fences old effects and completion grants even if lease numbers repeat. |
| Verification admitted while task projection is offline | Canonical work progresses once; mirror replay/rebuild catches up without redoing verification or holding a lease for the UI. |
| Telegram send acknowledgment lost | Visible unknown-send record; no blind retry; explicit replacement intent can be authorized with possible-duplicate notice. |
| Legacy PM mutation/cron targets a pilot binding | Command refuses or read-only projection; no JSONL-side dispatch, reminder or state mutation. |

The Telos-first and semantic-evaluation contracts add these acceptance cases:

| Case | Required result |
|---|---|
| A Telos node has no tasks-app binding | The node can still be planned, executed and verified; creating a tasks-app task is optional, and later binding must not duplicate the Telos node. |
| Add a high-unlock prerequisite or change boost | Owner frontier and explanation update; CLI and GUI agree at the same basis; PM/Laya/Jev maintain no duplicate ordering. |
| Several dependencies remain unsatisfied | Unlock count is not misrepresented as a count of immediately runnable tasks. |
| Duplicate create from CLI and repeated UI command | Same authorized command identity returns the same proposal/node; retry never accepts a proposal. |
| Unsupported new control attribute | Validator refuses until the owning schema/cardinality/behavior is added; descriptive tags do not grant execution semantics. |
| Planned feature becomes verified but is not released | Task, feature and spec-linked views show the distinct dimensions without publishing or inventing availability. |
| Disconnect and reconnect tasks-app↔Telos sync | Local tasks remain intact; Telos-owned fields catch up by binding/cursor; no duplicate nodes, lost local-only fields or silent conflict overwrite. |
| Command admitted before deadline, external effect delayed past capability expiry | Historical admission remains readable by authorized viewers, but effect admission rejects the expired execution capability. |
| Pending command replay after author revocation, with and without earlier admission | Earlier admission remains immutable history and causes no repeated effect; never-admitted command fails current-authority checks; relay cannot lend its privilege. |
| Python/TypeScript writer lacks a qualified generated codec | P0/P1 is blocked for that lane; no temporary JSON durable command store is created. |
| Existing integrations outbox and native command relay race | Same ledger/identity/recovery discipline and origin guard; no duplicate command ledger or legacy completion authority. |
| Required feature members are partial, empty, unknown or stale | One owner-computed versioned verified predicate gives the same true/false/unknown in CLI and GUI. |
| No authenticated release observation exists | Feature availability is not-recorded; synthetic fixtures are labeled and never count as a real release. |
| Old/new VO records, missing binding or unknown critical schema | Supported evolution preserves semantics; incompatible records are explicitly refused/migrated, never silently dropped. |
| Laya/Jev result is structurally valid but wrong schema or candidate set | Rejected before an effect; generated/TypeScript types alone do not pass runtime admission. |
| Relevant request changes during semantic evaluation | Result is retained historically, not used for current dispatch or automatic matching. |
| Model labels text as an approval | Only semantic data; cannot invoke privileged approval/commit or bypass authenticated authority. |
| Known model result replay vs crash before its admission | Known result is reused once; unknown attempt consumes budget and does not promise exactly-once third-party billing. |
| Provider unavailable, unauthorized data or Russian-language error | Core deterministic path remains available; visible abstention, no private-data fallback; measured per-language error. |

The matrix also covers disk-full/torn-write recovery, schema-version refusal, Unicode and large payloads, expired evidence URLs, clock skew, quota exhaustion, dead-letter replay, cancellation during an external effect, secret rotation, clean installation and browser keyboard/mobile regression. Each failure records the last durable receipt and the exact recovery boundary. No blanket `pass` is inferred from a process exit code or from mock output.

Additional interleaved-review and bilingual-site cases:

| Case | Required result |
|---|---|
| Patch advances during evaluation | Old response is historical only; no current warning/gate mutation. |
| Receipt or feedback redelivered | One accepted evaluation and at most one feedback action; no repeated launch or budget reset. |
| Evaluator timeout/unknown | Explicit abstention/unavailability, never a fabricated pass; final gates remain required. |
| Semantic pre-review misses a seeded defect | Independent final verification may fail; clean semantic output cannot override it. |
| Adversarial code comment requests approval | Untrusted data; no authority, policy or candidate-set change. |
| Frequent patches or many questions | Bounded/coalesced work, visible supersession and counted failed/stale calls. |
| Local output has valid enums but unreliable answers/probabilities | Syntax, accuracy and calibration are reported separately. |
| Related patches cross train/evaluation splits | Split guard prevents leakage; contaminated results cannot promote a model. |
| EN/RU switch on a deep link | Equivalent page/anchor and localized controls; English master remains normative. |
| Site exercises warning/stale/replay/final-failure | Real local fixture transitions only; no provider call or real work mutation. |
| Site bundle includes private transcript, secret or article PDF | Release check fails; only approved explanatory/public material is packaged. |

## 17A. Deterministic coding-agent coordinator delta

Danny's 2026-09 coding-agent notes sharpen P3 without introducing another coordinator-owned task database. The coordinator is an **execution plane**, not a planner. Telos remains the integrated graph owner for dependencies, checks, specification links and computed priority. tasks-app remains an independent task product with its own database and synchronizes bound work with Telos under section 10. An execution attempt references one admitted Telos node/binding and records operational state/receipts in AxE-owned durable state.

Source: [Coding agents most annoying things to fix](https://gist.github.com/vizanto/042a4e4702620c5c8dbbc15348901863).

### 17A.1 Attempt lifecycle

The minimum deterministic lifecycle is:

1. **Create** — allocate an attempt/worktree for one admitted Telos node, with an immutable attempt ID and explicit effect scope.
2. **Brief** — build the work order from `telos agent brief`, linked spec/source anchors and bounded code intelligence. Do not spend model turns rediscovering context that the graph/index already knows.
3. **Red** — establish or select the failing test/check before implementation when the task is behavior-changing and testable.
4. **Implement** — edit through the typed/structural path when supported; surface LSP, type and lint feedback immediately after each logical edit.
5. **Green** — rerun the smallest authoritative Nx target(s) that prove the local change.
6. **Mutation/adversarial check** — where the component supports it, prove the test/gate fails when the intended invariant is deliberately broken; otherwise record why this check is not applicable.
7. **Refactor** — simplify only while the same deterministic gates remain green.
8. **Rebase** — update onto the intended integration base before final acceptance. Conflict handling records ours/theirs/both plus type/symbol evidence; ambiguous semantic conflicts must not be silently guessed.
9. **Full gate** — run the declared Nx lint/test/build/release subset for the affected graph plus independent review/verification. Free-form agent prose cannot mark the attempt complete.
10. **Retro** — emit structured friction and improvement records: type friction, lint/gate friction, unnecessary context/tool calls, edit failures, flaky checks, repeated questions and simplification proposals. These feed the existing agent-ecosystem retrospective/Telos Registry workflow; they do not automatically weaken policy.

Every transition is reducer-driven and idempotent. A process crash or repeated delivery resumes from the last durable receipt rather than from chat history.

### 17A.2 Nx is the execution API, not a suggestion

Agents must not choose `bun test`, ad-hoc package scripts or partial shell commands when the repository declares the equivalent Nx target. Add one programmatic **Nx Op** (definition in §1.1) using Nx Devkit/programmatic APIs and expose it to HTS/coordinator callers. It returns structured run/task identity, state, timing, cache/result metadata and an output reference; individual task output remains fetchable after the run.

The operation must preserve the project graph and affected/dependency semantics. Infrastructure failure, tool bootstrap failure, source failure and policy failure are distinct result classes. The coordinator may choose a smaller valid target set for the inner red/green loop, but the final gate is declared by repository policy, not invented by the model.

Tracked implementation: **HYP-1510**.

### 17A.3 Questions are typed state, not conversational control flow

Each agent question gets a durable question ID bound to the attempt and Telos node. A deterministic reducer classifies the next action:

- **answer now** from reducer/Telos/session context when the answer is already authoritative;
- **queue** when the information is useful but should not interrupt the current tool/model turn;
- **interrupt** only for genuine human authority, missing product intent, credentials/effect approval or unresolved ambiguity.

A model or semantic judge may help rank/retrieve context, but cannot grant privileged authority. Replies are idempotent by question ID and can arrive through tg/harness adapters without depending on tmux identity. The same protocol covers task updates that should be queued until a turn boundary.

Tracked implementation: **HYP-1511**.

### 17A.4 Context and edits use code structure before model tokens

Context gathering prefers Telos source links, code/symbol indexes and typed graph queries over repeated file-reading tool calls. HTML/spec surfaces should visualize the types, events and messages that define the attempt boundary so the human and agent can inspect the same contract.

For supported changes, the code-intelligence layer exposes AST/symbol/type-graph operations, bulk-edit detection and immediate LSP/lint/type diagnostics through typed Ops usable by HTS/coordinator code. Range/text editing remains a fallback, not the default for mechanically structured edits. A fast model may repair a mechanical edit/tool failure, but cannot bypass a failed semantic/type/build gate.

Tracked implementation: **HYP-1512**.

### 17A.5 Semantic judges stay bounded

Laya (default) and Jev (secondary/fallback/comparison) decisions are useful for semantic linting, code-quality/performance heuristics, question/context classification and shadow real-time review between expensive model calls. They remain advisory until a separately measured promotion decision. They cannot approve commits, privileged effects, acceptance criteria or release readiness.

Rules supplied to semantic linting should be versioned and attributable (for example performance-handbook rules). Results record the exact artifact/context/model/rule version and support abstention. Error-rate reduction or fine-tuning is evaluated on held-out local examples rather than vendor demonstrations.

### 17A.6 Split work and route models/accounts from measured state

The coordinator should detect independent failing tasks and split them instead of serializing them onto one agent. It can use estimated token/tool budget, provider/account availability, recent error classes and measured throughput to choose an execution lane; Laya may supply a bounded semantic estimate, but deterministic routing constraints remain ordinary code. High-throughput/low-latency models are preferred for mechanical or well-bounded work; difficult semantic work escalates deliberately.

Running out of one model/account is an explicit routing event, not a reason to abandon the attempt. Retry, split and escalation budgets are bounded and visible. Parallel agents may not write the same contended artifact unless the coordinator establishes a single-writer/merge protocol.

### 17A.7 Pilot on AxE/Conloca release work

This work must not displace the current business priority. The first connected evaluation uses one bounded AxE/Conloca release task after the required primitives are ready, rather than a new demo product. Measure human interruptions, free-form chat turns, repeated requirements, stale/failed attempts, edit/tool failures, Nx task failures, orchestration overhead, model/tool wait and time to verified result. Compare against the current workflow and publish an expand/revise/stop decision.

Tracked implementation: **HYP-1509** (coordinator), **HYP-1513** (pilot). The broader coding-agent PRD remains **HYP-23**; **HYP-858** remains the existing ecosystem-retrospective stream and is reused rather than duplicated.

## 18. Milestones and implementation ownership

| Milestone | Deliverable and exit gate | Primary owners |
|---|---|---|
| P0 — Runtime and contract qualification | Pinned clean Linux/ARM64 native build; AxE-primary Telos; real atomic lease/fencing and timer-expiry probes; qualified generated Python/TypeScript VO client paths and old/new fixtures; actual container isolation; no production connections. | RIG/agent-tools; AxE/Containium/VO owners; operator. |
| P1 — Native intake and domain commands | Scoped intake revisions and idempotent Telos proposals/admission; explicit tasks-app↔Telos bindings, field ownership and sync receipts; standalone tasks-app/task-cli behavior remains intact. | Telos model/client owners; tasks-app/task-cli; tg-cli adapter; PM integration. |
| P2 — Task and feature synchronization/views | tasks-app remains a writable independent DB while bound records synchronize with Telos; frontend/backend feature catalog, spec-heading links, computed priority/unlocks, explicit boost, evidence/release dimensions and anchored review feedback. Offline catch-up and conflicts are tested. | task-cli; tasks-app; Telos sync adapter; review-cli. |
| P3 — Deterministic execution | Real AxE/PM attempt protocol, one bounded tool worker, independent verification, canonical completion and eventual UI update. Kill-switch, privacy fallback, stale-worker, duplicate and restart gates pass. | Telos/PM agents; harness/supervisor; review/QA. |
| P4 — Bounded model assistance | Limited coding plus independent review/QA; optional shadow intake/matching and interleaved semantic code pre-review with pinned artifact/context/model, validated responses, abstention and replay. Promotion requires held-out local results, not vendor benchmarks. | PM policy; permitted model adapters; reviewers. |
| P5 — One connected isolated workflow | Approved chat/topic and dedicated task UI scope; one real request represented canonically in Telos; complete receipts and restore tests. No production merge or deployment. | Operator and integration owners. |
| P6 — Evaluation and consolidation | Compare practical benefit, integrity and complexity; decide further changes or expansion. Repository consolidation remains a separate decision. | Alex and Danny with measured evidence. |

Each milestone requires a versioned acceptance report and exact basis. A working UI does not substitute for native readiness; source targets do not prove runtime behavior. Upstream changes belong to their owner repository, and integration glue stays thin. No mock loop, JSONL-side planner or extra task database is introduced because native packaging is harder than expected.

Dependency-ordered implementation work is: qualify runtime/VO transport; correct scoped source identities; define native admission and field/check gaps; define tasks-app↔Telos binding/field-ownership/sync contracts while preserving standalone modes; implement PM AxE attempts and verification; add graph queries; validate human surfaces and recovery. Semantic judging is optional to this core path; Laya is the default judge when enabled, Jev is secondary/fallback/comparison, and both start without authority over hard gates. Optional external issue adapters are later, explicitly scoped work, not a prerequisite to native task management.

Existing PM state-refresh issue #3 and partial-fetch issue #4 remain relevant to legacy source observation; reference them rather than silently treating current onboarding as a synchronization engine. Child implementation work is to be represented in the same Telos dependency graph when that environment is qualified, not as another manually ordered backlog. This document does not claim those nodes or child tickets have already been created.

## 19. Measurement and acceptance

Compare the same small workload against the existing manual/CLI workflow and the pilot. Record number of human interventions, unnecessary questions, repeated requirements, duplicate canonical work, false completion, successful recoveries, time to identify the blocker, time to verified result, model invocations, verifier time and operational setup effort. Also measure manual reprioritization avoided, requirement-to-feature traceability and whether the same planned/verified/released state appears in CLI and GUI without manual synchronization. Separate tool/model wait from orchestration overhead. Current PM quota/usage observations do not establish monetary spend, so do not invent cost savings.

Hard correctness gates are zero lost admitted requests, zero unauthorized effects, zero duplicate canonical work in the duplicate scenarios, no obsolete-worker completion, and recoverable source/graph state across all exercised crash boundaries. These are claims about the specified experiments, not proof against every possible failure. Any unexecuted matrix case remains unverified and blocks the milestone it guards. A short successful demo is not a reliability measurement.

Provisional responsiveness targets for the fixture lane are p95 durable intake acknowledgment under one second and p95 visible projection refresh under two seconds after acknowledged input, excluding model and external-provider latency. Measure at least 100 fixture inputs and report the raw distribution, hardware/runtime, concurrency and failures. Run at least ten restart/restore cycles spanning distinct crash boundaries, including a mixed-store checkpoint attempt, an unknown external effect and a still-alive pre-restore worker. These are proposed evaluation targets, not current benchmarks. Small samples support a pilot decision, not broad statistical claims about production availability.

Expansion requires a report showing both practical benefit and bounded complexity. A faster graph query cannot compensate for lost requests or a second independent authority. A more reliable workflow may justify modest orchestration overhead; publish the tradeoff. A failed performance target produces a measured investigation, while a failed authorization, isolation or integrity gate prevents connected execution until corrected. The report includes rejected alternatives and the minimal next change, not an automatic mandate to merge repositories.

## 20. Recovery, rollback and ownership transfer

Use a quiesced checkpoint rather than an assumed distributed snapshot. Close new intake/dispatch and the independent effect gate; hold workers; classify in-flight operations as confirmed, failed or unknown; settle or retain pending command/delivery receipts; then freeze relays. Record the AxE basis, tasks-app's own-data/outbox snapshot boundary, artifact generation, optional graph/projection bases, schema identities and image/configuration digests. Do not seal while an authoritative store still changes. Unknown external effects remain explicit and are not blindly replayed.

Restore into a fresh isolated environment with effects disabled and a new unpredictable environment incarnation created outside the restored snapshots. Install that incarnation at gateways and execution boundaries before re-enabling writes. Old workers cannot become current just because a restored numeric lease generation recurs. Historical evidence retains its identity; old worker permissions are not automatically resumed.

After restore, AxE/Telos remains authoritative for Telos-owned integrated graph semantics and tasks-app PostgreSQL remains authoritative for tasks-app-owned/local fields. Rebuild minigraf from the restored canonical basis, restore tasks-app from its own database snapshot, then reconcile bound records by stable binding, cursor and field-ownership rules. A missing Telos-derived field is resynchronized; a missing tasks-app-only field is restored from tasks-app, not inferred from Telos. Neither side fabricates completion or silently overwrites a genuine conflict.

Reconcile durable pending UI/source commands by their original command IDs and Telos admission receipts. An unknown command is resubmitted idempotently under its original identity and current admission checks, not inferred accepted from a cached row. A confirmed canonical operation is not rerun because its GUI acknowledgment was lost. Irreconcilable missing log/receipt data blocks resumption. Optional external effects follow their own retained provider receipts; projection parity alone cannot prove an external write occurred.

Rollback of code is not rollback of external effects. Stop new writes, reconcile in-flight work, preserve logs/artifacts and use a schema-compatible reader or explicit forward repair. Old/new VO fixtures establish supported evolution; an older binary must not reinterpret unknown critical semantics. Removing a pilot adapter does not delete original requests or mutate production bot configuration.

At handoff record the worktree, commit/dirty state, tracking issue, document hash, source versions, owned process handles, pending operations and exact next checks. Diagnose owned processes with PID/PPID/elapsed/command name, not secret-bearing argv. Never reset a shared checkout, stage another agent's files, broadly kill sessions, weaken a hook or bypass a blocked action. UI-only review deliveries that lack a live agent session remain durable pending work and must be read explicitly; a guessed tmux target is not reliable delivery.

## 21. Decisions that remain genuinely open

The default design is one isolated native AxE-primary environment, separate current repositories, synthetic sources first, and no production credentials. Engineering questions such as Linux artifact compatibility, the exact minigraf binding, durable event-feed support and atomic lease admission are resolved by code inspection and probes, not by asking the human to guess. Their failures produce specific owned fixes.

Before connected mode, the operator explicitly selects the permitted Telegram bot/chat/topic, tasks-app workspace, disposable repository and credential scope. Before model-assisted mode, the operator selects allowed model routes and confirms the recorded resource/iteration budget. Before any later repository consolidation, Alex and Danny review the evidence and choose ownership/build boundaries. These are authority decisions; this specification does not pre-answer them.

## 22. Source references

These references justify observed baseline descriptions, not the proposed behavior. Exact inspected revisions and test coverage are in the companion evidence record. Normative integration requirements above remain proposals until implemented and accepted.

- **S1 — AxE runtime and Telos implementation:** [axe.sc/axe at 0f3e99779206159ee0aa3601ee4e9bc010bf53eb](https://git.hyperide.ai/axe.sc/axe/src/commit/0f3e99779206159ee0aa3601ee4e9bc010bf53eb), especially `CLAUDE.md`, `AGENTS.md`, `packages/telos-cli/src/telos_cli/backend.py`, `serve.py`, `cli.py`, `packages/telos/src/signals.ts`, `decide.ts`, `targets/telos-service/package.json`, and package manifests for Containium, VO, Hero and IAM.
- **S2 — Telos concepts, graph and store ownership:** the same AxE revision, `specs/telos/00-concepts.md`, `85-v0-contract.md`, `86-v1-architecture-rollout.md`, `87-backends-identity-and-migration.md`. Conceptual descriptions are reconciled with the current schema and admission code before implementation; stale comments are not a wire contract.
- **S3 — PM baseline:** [pm-cli at 386cefe05a7880b3080c2cba5b8a335f43e2838f](https://git.hyperide.ai/ultrabricks/pm-cli/src/commit/386cefe05a7880b3080c2cba5b8a335f43e2838f), `AGENTS.md`, `README.md`, `pmlib/adapters/task.py`, `pmlib/adapters/telegram.py`, and their existing unit tests.
- **S4 — Task CLI model and adapter seam:** [task-cli at c663520e6335b90e16d0f16fc9df4efafc3db89a](https://git.hyperide.ai/ultrabricks/task-cli/src/commit/c663520e6335b90e16d0f16fc9df4efafc3db89a), `tasklib/model.py`, `tasklib/backends/__init__.py`, and `README.md`.
- **S5 — Tasks application API and integrations:** [tasks-app at 501cce5568ec1137458bb012fe968150fe21583f](https://git.hyperide.ai/ultrabricks/tasks-app/src/commit/501cce5568ec1137458bb012fe968150fe21583f), `docs/openapi.json`, `package.json`, `packages/server/modules/integrations/service.mjs`. This is an observed source revision, not verification of the deployed application.
- **S6 — Communication, harness and spec review:** `tg-cli` at `469033fe2c768542e55d832d97502de375598f87`; `harness-cli` at `6a9ae0673db13881c91c6fbc27f3e1c8270adc84`; `review-cli` at `156f4ad5d4e0ba3d71a65102a7eefefce652bda7`, particularly each README and the live spec-web help/status. All are under [ultrabricks on Forgejo](https://git.hyperide.ai/ultrabricks).
- **S7 — minigraf:** [hyperide/minigraf](https://git.hyperide.ai/hyperide/minigraf), live README retrieved through authenticated Forgejo CLI; remote HEAD observed as `5721d6611894c938d6c62a3784da7c62a81d7f24`. Embedded architecture and sidecar caveat are source observations; performance and binding compatibility were not independently measured.
- **D1 — Container service controls:** [Docker Compose service reference](https://docs.docker.com/reference/compose-file/services/).
- **D2 — Secret delivery:** [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/).
- **D3 — Daemon privilege model:** [Docker rootless mode](https://docs.docker.com/engine/security/rootless/).

- **S8 — Telos priority, attributes and specification links:** AxE at `0f3e99779206159ee0aa3601ee4e9bc010bf53eb`, `packages/telos-cli/src/telos_cli/derive.py` (`structural_frontier`, `frontier`, `unlocks`), `model.py` (`CARDINALITY`, `TAG_PREFIXES`) and `specs/telos/88-literate-spec-linking.md`. Code-governed control attributes differ from open descriptive tags; native parity still requires tests.
- **S9 — VO wire/storage evolution:** the same AxE revision, `specs/containium/09-wire-format.md`, `specs/vo/03-schema-identity.md` and `04-wire-format.md`. Normative format intent is distinguished from their explicit implementation gaps and draft evolution contracts.
- **T1 — TypeSafe research:** [Jev/AxE analysis](https://spec-dev.hyperide.ai/spec/rig-axe-typesafe-decision-research), with current official API, primitives, model limitations, confidence semantics and public SDK source references. No paid/live inference was performed.
- **T2 — Laya:** [he-jev/laya](https://github.com/he-jev/laya), the current default semantic-judge implementation selected for this pilot. Selection establishes routing preference, not correctness/authority; local qualification remains required.
- **R1 — Danny's review:** [comment dispositions](https://spec-dev.hyperide.ai/spec/rig-axe-pilot-danny-review), preserving the three source comment IDs and what changed. The source export and live spec-web store were read; answered is not reviewer-resolved.

**Acceptance rule:** this document being published, reviewed or approved does not mean the pilot has run. A milestone is accepted only against its own versioned evidence and the scope limits above.
