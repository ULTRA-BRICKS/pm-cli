# RIG + AxE — Architecture review receipts

The following are independent design-review reports, not executed runtime tests. Reviewed master SHA256: `a65970de2713a42cc31dc080fe918d642826277af88383933823bcf73f26ff38`. Both explicitly selected routes completed successfully. Their proposed fixes require author assessment; the recommendation to replace an already admitted receipt after revocation was not adopted because it would rewrite historical admission. The revised contract separates immutable receipt lookup from current authority for new effects.

## claude:claude-opus-4-8 [ok]


This is a genuinely strong, unusually self-critical spec. Most of the boundaries Danny asked for are already drawn well (I acknowledge those explicitly at the end). Below are the four highest-impact places where the architecture still contradicts itself or leaves an owner/predicate undefined.

## Finding 1 — The "released" dimension has no authority owner (§5 vs §6 / §15.1)

**Failure sequence.** §6 and §15.1 make "released availability by environment/version" a first-class feature facet, and §5/§15.1 route it to "release receipts." But the §5 ownership table ("one authority per domain") has **no row that owns release/deployment truth**. Artifact storage owns "verification receipts"; nothing owns *release* state. So the feature catalog surfaces a dimension whose source of truth is unassigned. A projection with no declared authority is exactly the "second truth" failure mode this spec exists to prevent — a UI or an integration could populate "released" from a merge event or a spec note, and there is no owner to refuse it.

**Minimal correction.** Either add a §5 row — *Release/deployment state → owned by [deployer/release verifier], others may link receipts, never derive Done* — or, since real releases are an explicit non-goal (§2), state plainly that in-pilot "released" is always `none`, has no writer, and release receipts are out of scope. Right now the dimension exists without a guardian.

## Finding 2 — VO is mandatory, its Python/gateway binding is a known gap, and the only escape is forbidden (§7, §12, §13.1, P0/P1)

**Failure sequence.** §7/§12 require *all* new internal protocols and durable records to use VO, and forbid "another JSON protocol," "hand-written codec," or private JSONL — while the evidence note and §12 record that VO's **client/Python/gateway paths are explicitly incomplete**. The escape hatch offered is "invoke an existing generated client through a supported boundary." But pm-cli and the tg-cli intake adapter are **Python** and sit on the P1 critical path, and the Python binding is precisely one of the listed gaps. So the escape hatch depends on the thing that is missing: Python components must speak VO → VO has no Python codec → the sanctioned workaround (generated Python client) is itself unimplemented → and the unsanctioned workaround (JSON) is prohibited. P1 cannot legally serialize anything.

**Minimal correction.** Make the Python access path concrete and name its owner as a P0 *exit gate*: either commit to delivering a generated Python VO client, or keep Python out of codec-land entirely by having it call a VO-native gateway over that gateway's already-supported wire. Also state P1's behavior if the codec slips (blocked, not "temporary JSON"). "Implement at the owner" is not a path when the owner's gap is the blocker.

## Finding 3 — Feature-level "verified" has no defined aggregation predicate (§15.1, §16)

**Failure sequence.** A feature "can link several implementation/check nodes without copying their statuses" (§6/§15.1). §15.1 correctly forbids fake completion percentages — but never says **when the feature-level `verified` dimension flips true**: all gating nodes satisfied, any, or a weighted subset. Because the rule is undefined, two renderers (task-cli vs tasks-app, §18 P2) can compute "verified" differently from the same graph at the same basis — directly contradicting the acceptance case "CLI and GUI agree at the same basis" (§17) and re-introducing divergent feature truth by the back door.

**Minimal correction.** Define `verified` as a declared, versioned predicate over the linked subgraph (e.g., "all nodes tagged gating are `done` at current basis; non-gating nodes are informational"), owned by the view definition, so every surface derives the same boolean. Freshness invalidation (§9) then applies to that predicate, not to ad-hoc per-UI logic.

## Finding 4 — Pending-command reconciliation re-check omits current authorization of the original principal (§7, §20)

**Failure sequence.** §7's tasks-app outbox durably stores a command + local receipt; §20 reconciles unknown commands "idempotently under its original identity and **current admission checks**." But the enumerated admission checks (§7: "schema, workspace, allowlist, expected revision and idempotency") do **not** include re-evaluating whether the *original authenticated principal still holds permission* at replay time. §8 re-checks permission for dispatch and §9 gives approvals a nonce+expiry — but a structural create/propose command sitting in the outbox has no expiry and no stated authorization re-evaluation. A command authored while a principal was authorized, then replayed after that principal's access was revoked, can be admitted on the strength of its captured identity.

**Minimal correction.** Add current-principal authorization (not just workspace/allowlist membership) to the reconciliation re-check, and give durable pending commands a bounded validity/nonce like approvals, so a revoked author's stale outbox entry fails closed rather than replaying. Low blast radius (a staged proposal still needs §9 to commit), but the boundary is currently unstated.

---

## Concerns you asked about that are already well covered — do not re-open

- **Duplicate task truth:** §5's "one authority ≠ one database" carve-out, "a projected node never implies an unobserved command was admitted," and the drop/rebuild-projection acceptance cases (§16/§17) handle this cleanly.
- **Lease/fencing across restore (numeric generation reuse):** §8 and §20 explicitly fence "even if lease numbers repeat" via a fresh environment incarnation installed before writes re-enable — thorough.
- **Evidence vs current (stale receipt after artifact change):** §9 freshness/invalidation plus the §16 second-slice test are exactly right; not a gap.
- **Model output validation / retries / privacy:** §8.2 and research §5–7 already require response-schema validation over the SDK cast, SDK retries set to zero under AxE, body-log suppression, no privacy-crossing fallback, and VO-backed receipts. No further boundary needed.
- **Native runtime vs SDK async:** the Op-in-I/O-realm rule ("never `client.systemOne` inside hermetic decide") correctly confines SDK async to the Op; the archived `11c` async spec is *not* relied upon — the generator-yield path is. Covered, with the one residual caveat that the Op's replay/retention contract is deferred to "that mode's contract"; worth pinning in P0 but not a standalone defect.

One process note: I answered from the diff only, per the read-only review constraints — no files inspected, nothing executed, and (correctly) none of the runtime probes the evidence doc itself flags as unrun should be treated as passing.

---

## oc:zai/glm-5.2 [ok]


Grounding check done: the four documents exist in `docs/`, and the evidence record's source claims verify against the code (`plan_ingest` dedupes with no state refresh — `pmlib/adapters/task.py:20-23,91-92`; `plan_capture` builds bare `tg:<id>` — `pmlib/adapters/telegram.py:42`). Reviewing the documents' architecture, here are the four highest-impact findings.

## F1 — Idempotent command replay vs revoked/expired authorization

**Sections:** master spec §7 ("Retrying the same command returns the same admission/proposal receipt"), §9 (approvals bind actor/scope/expiry/nonce), §20 ("An unknown command is resubmitted idempotently under its original identity and current admission checks").

**Contradiction:** §7 promises same-identity → same-receipt unconditionally; §20's "current admission checks" implies the outcome can change; neither defines which wins when the original admission succeeded but its response was lost and the authorization basis (nonce expiry, pause latch, revoked permission) has since changed.

**Failure sequence:** Human approves via callback → relay submits command C → Telos admits C, response lost → relay retries C after the approval nonce expired or the stop latch closed. Either (a) Telos returns the original admission receipt — revoked authorization materializes as admitted work — or (b) Telos re-evaluates and rejects — one identity now has two outcomes, which §7 forbids, so the relay misclassifies it as a body conflict or treats the pending row as admitted. The §17 gates cover lost-response reconciliation and stale *buttons* separately, never their intersection.

**Minimal correction:** Amend §7/§20: same-receipt idempotency holds only while the authorization basis recorded at first admission still holds; otherwise replay under the same identity yields a terminal, recorded `rejected: authorization-basis-changed` receipt (superseding, not re-evaluated, not conflict). Add that case as a §17 matrix row.

## F2 — VO mandate vs unimplemented codecs at the P1/P2 durable-write boundaries

**Sections:** §7/§12/S9 (VO default, "no new ad-hoc JSON envelope… for durable domain records"), §8.1 (`pmlib/adapters/axe.py`), §18 P0/P1; evidence record ("VO client/Python/gateway coverage is explicitly partial").

**Missing boundary:** The three durable writers on the critical path — the tasks-app pending-command outbox, its relay, and the Python `axe.py` client (under this repo's stdlib-only-at-import rule, so any codec is a lazy dep) — have no qualified codec lane today. §12's escape hatch ("implement it at the owner or invoke an existing generated client") names no owner, no milestone dependency, and crucially no interim rule for durable writes.

**Failure sequence:** P1 starts before VO owners ship Python/gateway codecs → either P1 blocks with no owned escalation path, or pending receipts/admission results get persisted as "temporary" ad-hoc JSON — exactly the second internal authority Danny's remark #3 and §7/S9 forbid — which then becomes legacy data requiring §20 migration.

**Minimal correction:** Add to §7/§12: no durable pilot record may be created in a lane whose VO codec is unimplemented at the writing boundary (those flows stay fixture/in-memory); make the missing Python/gateway codecs a named cross-repo dependency on P1's exit gate so "codec missing" is a blocked milestone, not a local workaround.

## F3 — Two command ledgers inside tasks-app

**Sections:** §7/§10 (new "tasks-app pending-command outbox") vs evidence record (existing integration service "persists inbound webhook deliveries with scoped deduplication and contains an outgoing outbox path" + GitHub merge-to-Done); §20 (singular "tasks-app's own-data/outbox snapshot boundary").

**Missing boundary:** The spec never states whether the pending-command outbox extends the existing integrations outbox or is a second ledger in the same service. §5 forbids duplicate authority *across* components but is silent *within* tasks-app.

**Failure sequence:** Pending command committed to the new outbox while the existing integrations outbox is mid-flight for the same node → crash between commits → restore classifies in-flight operations under two unreconciled retry/dedup/cursor disciplines; or both relays deliver for one user action and merge-to-Done fires on a native view — the §17 gate catches the write only if origin-scoped routing provably covers the *existing integrations* routes, which is specified as one sentence ("Routes resolve entity origin server-side") with no route inventory.

**Minimal correction:** §7/§10: the pending-command outbox either reuses the integrations outbox (one durable command ledger, one recovery discipline) or is separate with a declared reason; §20 enumerates both snapshot boundaries; P2's exit gate names the existing integrations routes that must gain origin-scoped refusal.

## F4 — The evidence chain of the documents themselves is self-referential

**Sections:** Danny doc §5 ("Reply receipt verification is recorded in the companion evidence record after the commands run"); evidence record ("…its new verification receipts are recorded below" — but the section below contains only prose, no reply IDs, transcripts, or document digests); master spec §3 + acceptance rule.

**Failure sequence:** Any later reader or the P0 gate tries to verify the three remarks were answered in the live store and that the revised text is what was reviewed → the evidence record cannot distinguish observed-store fact from author claim (violating its own §3 evidence classes); the initial panel's "six of nine routes, three failures" is likewise unreferenced; under the ship-discipline loop the Danny threads remain answered-but-unproven, and the acceptance rule ("a milestone is accepted only against its own versioned evidence") has no instantiated binding mechanism.

**Minimal correction:** Append the actual reply receipt/comment IDs, the reply-command transcripts, and a SHA digest of each revised document bound to those receipts; state that revision-digest ↔ evidence-entry binding as the mechanism behind the acceptance rule.

## Explicitly acknowledged as already covered (not re-raised)

- **Duplicate task truth (Telos vs task-cli/tasks-app):** §5/§10/§10.1 + the "native work has no tasks-app issue" gate. The pm-cli residual (no state refresh on re-ingest — confirmed in `adapters/task.py`) is owned via issues #3/#4 and out of pilot scope.
- **Evidence/current generation:** §9 freshness-by-input-change, slice 2, "artifact changes after review or QA" gate; lease/environment-incarnation fencing (§8.1/§20) covers generation recurrence after restore.
- **Feature verified ≠ released:** §15.1's distinct dimensions plus a dedicated gate row.
- **Model output validation/retries/privacy:** §8.2 and research §§5–7 cover the SDK cast-not-validate, retry multiplication, body logging, and zero-data-retention caveats (its VO-backed receipt inherits F2's codec gap — that's why F2 extends past P1).
- **Native runtime vs SDK async:** research §3 separates pure-Op vs generator modes and flags per-mode replay/retention testing on the real runtime.
- **Telegram `tg:<id>` scope gap:** confirmed at `adapters/telegram.py:42`; already recorded as a source-level gap with the spec requiring scoped identity before connected mode.
