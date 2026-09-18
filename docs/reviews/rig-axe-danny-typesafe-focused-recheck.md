# RIG + AxE — Focused correction recheck

This is a focused review by three roles of the same explicitly selected model, not three independent models or runtime tests. Reports below assessed the cited excerpts. The author subsequently clarified receipt viewer authorization, DeliveryRecord versus immutable AdmissionReceipt, P0-to-P1 dependency, execution-capability expiry, and the release-observation verifier. Those final clarifications were author-checked, not followed by another model-review round.

## claude:claude-sonnet-5 [quality] [ok]


**Note on scope:** This document is a prose specification, not code — there's no diff of source files here, so a "code quality: readability/naming/duplication/idiomatic usage" review doesn't apply. That closing instruction reads as mismatched with the actual artifact under review; I'm treating the task as originally framed (a design-consistency recheck) rather than following it, since acting on it would just be reviewing text that isn't code.

## Assessment of the corrections

The four listed prior issues are genuinely closed:
- **Verified aggregation** is now defined as a tri-state (`true`/`false`/`unknown`) computed once by a versioned projection contract, with explicit rules for empty/undeclared scope → `unknown` rather than vacuous success.
- **Python VO codec path** now names a concrete owner (`packages/vo` → `packages/axe-client-python`) and explicitly forbids JSON persistence as a fallback while blocked.
- **Pending-command authorization** is no longer stale: admission re-checks current principal permissions, and receipt retrieval is explicitly barred from re-admitting or rewriting history.
- **Duplicate outbox** is resolved by declaring one owner (extends the existing integrations outbox) and separating inbound webhook-delivery receipts from outbound commands by role, not by a second ledger.

The revocation-vs-history split (revocation blocks new effects, not history) is also coherently implemented via two enforcement points — admission-time check and dispatch/effect-boundary check — rather than mutating the historical receipt.

## Two remaining HIGH-impact findings

1. **Whose authorization gates receipt reads is unspecified, and it can silently defeat the "history is immutable" guarantee.** §7.2 says "current read authorization still governs which receipt details may be returned," but doesn't say whether that's the *viewer's* access-control check or a re-check of the *original principal's* (possibly now-revoked) permission. If it's the latter, a revoked/expired principal's receipt could become unreadable/hidden after the fact — which is functionally a rewrite-by-omission of history, contradicting the explicit design intent that revocation blocks only new effects. This needs one sentence clarifying that read authorization is evaluated against the requester's own current viewing rights, not the original actor's now-current status.

2. **Two objects are both called "receipt" with unclear identity/authority relationship.** §15.1's outbox stores a "local pending receipt" that the relay later updates with "the admission result," while §7.2 describes an "already admitted receipt" retrieved from Telos as immutable history. The spec never states whether the outbox's locally-updated receipt *is* the authoritative admitted receipt (i.e., a cached copy that must exactly mirror Telos's record) or a separate client-side artifact that could drift from it. Given §7.2's immutability guarantee is specifically about the admitted receipt, this ambiguity matters: without an explicit "outbox receipt is a cache of Telos's admission result, never authoritative" statement, two components could disagree about what happened and both call it a "receipt."

Both are small clarifications, not structural rework — the corrections otherwise hold up.

---

## claude:claude-sonnet-5 [correctness] [ok]


## Assessment

The four corrected protocols are internally consistent for what they were built to fix:

- **Revocation vs. receipt reads**: 7.2 cleanly separates "receipt retrieval" (read-only, replays history, gated only by *current read authorization*) from "revocation/pause" (a cancellation input enforced at dispatch/effect boundaries). This matches the stated intent — later revocation blocks new effects, not historical reads — and correctly rejects the retroactive-rewrite alternative.
- **No JSON fallback**: explicitly closed — "temporary JSON persistence is not a fallback" plus P0/P1 gate blocking durable native writes until codec conformance passes.
- **One outbox**: explicitly closed — "extends the existing integrations outbox... does not create an independent command ledger," with webhook-delivery receipts kept as a separate, non-reinterpreted provenance record.
- **No fake released state**: explicitly closed — `not recorded` is the real state, synthetic receipts must be labeled fixtures and excluded from real-availability claims, and verified≠released is stated twice (15.1 overview and the release-owner paragraph).

Two remaining concrete issues:

1. **P0/P1 gate-label mismatch (7.2).** "P0 exits only after those exact writing/reading boundaries pass codec and stored-history conformance. Until then... fixtures... do not satisfy P1." The exit criteria are framed as a P0 exit condition, but the very next clause switches to gating P1 on the same fixtures, without stating the P0→P1 relationship. As written, it's ambiguous whether Python codec conformance is a P0 exit criterion or a P1 entry criterion — which determines whether the pilot can legitimately ship P0 without it. Worth tightening to one consistent gate label.

2. **Deadline re-check omitted at effect boundary (7.2).** Revocation/pause is explicitly "enforced at dispatch and effect boundaries," but the validity-deadline check is only described at "first admission" time. If an effect is delayed past the original deadline after admission, nothing in the text re-checks expiry the way it re-checks revocation — an admitted-but-now-expired command could still produce an effect. This is an asymmetry between the two cancellation-like inputs (revocation is re-checked late, expiry is not) and is worth an explicit statement either way (deadline re-checked at effect too, or deliberately admission-time-only by design).

---

## claude:claude-sonnet-5 [performance] [ok]


## Assessment

The four corrections do resolve the originally listed issues: the Python VO path is now concrete and owned (`packages/vo` → `packages/axe-client-python`, schemas owned by `packages/containium-wire-schema`), pending-command authorization is re-scoped to *current* permissions at first admission while explicitly preserving admitted receipts as immutable history, the duplicate-outbox ambiguity is resolved (webhook-delivery receipt is explicitly "referenced rather than reinterpreted" as a command), and feature-verified aggregation now has a single named computation owner (the Telos-side projection contract). Revocation-vs-receipt-reads is handled cleanly: admission checks use current authority, but retrieval of an already-admitted receipt "does not re-admit" and revocation is enforced only "at dispatch and effect boundaries" — this matches the stated immutable-history intent. "No JSON fallback" is explicit ("temporary JSON persistence is not a fallback"). "No fake released state" is explicit (`not recorded`, synthetic receipts must be labeled fixtures excluded from real-availability claims).

Two remaining HIGH-impact gaps:

1. **Release-observation owner is asserted but never named.** Unlike the outbox ("one delivery/recovery owner") and the VO codec ("AxE-owned `packages/vo`"), §15.1 only says released availability "has a different owner" without identifying it. Since the pilot has no real deploy/release writer, this is latent now, but it reproduces the exact "undefined owner" failure mode the original review flagged for feature-verified aggregation — whoever eventually implements the release-observation writer has no designated owner to bind to, risking the same ambiguity resurfacing at P2+.

2. **"Local pending receipt" (outbox) vs "admitted receipt" (Telos, §7.2) are not clearly distinguished as separate objects.** §7.2's immutability guarantee applies to the Telos-issued admitted receipt. The outbox section says the relay "records the admission result and updates the projection" after submitting — it's not stated whether this update mutates the outbox's own local pending receipt or merely stores a pointer to the separately-immutable Telos receipt. If the former, it risks conflating a mutable delivery-tracking record with the immutable historical receipt the revocation model depends on; the spec should explicitly name these as distinct artifacts.
