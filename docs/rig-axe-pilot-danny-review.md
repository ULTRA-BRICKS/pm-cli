# RIG + AxE Pilot — Danny Review Dispositions

**Reviewed:** 2026-09-18. **Source:** `/Users/ultra/Downloads/denny-rig-axe-pilot-master-spec-comments.json`, batch `2026-09-18T12:09:38.874203+00:00`, checked against the actual spec-web comment store. **Master:** [open the revised specification](https://spec-dev.hyperide.ai/spec/rig-axe-pilot-master-spec). **TypeSafe:** [research and decision-loop integration](https://spec-dev.hyperide.ai/spec/rig-axe-typesafe-decision-research).

The three remarks are accepted as design corrections. This document distinguishes editing the specification, answering a review thread and implementing the pilot. A reply does not resolve a remark on Danny's behalf or demonstrate runtime readiness.

## 1. Telos is the work model, not a neighbor to another backlog

**Comment:** `1d5c980c6c26434891baa1e69829c8c8`, anchored to section 5's tasks-app ownership row. Danny explains that Telos computes useful next work from the graph, supports manual boost, carries task fields/checks, and can absorb useful task-cli capabilities.

**Disposition:** accepted. Sections 5, 7, 10, 16, 18 and 20 now make Telos authoritative for pilot-native work. task-cli is a command interface; tasks-app holds task/feature projections, pending command receipts and its own UI/identity data, not a competing work lifecycle. There is no mandatory creation of a second issue or distributed completion transaction merely to display a Telos node. Existing external/legacy issue flows remain separately scoped and are not migrated.

**Code-grounded qualification:** inspected `derive.py::structural_frontier` orders by boost, criticality, urgency, freshness nudge and ID, while `unlocks` provides dependency closure. We preserve the owner's actual algorithm rather than replace it with a simplistic count or model-generated ranking. Some nearby prose still describes an older ordering; native/Python conformance remains untested. `model.py` validates the control-attribute vocabulary; its open descriptive namespaces do not affect readiness. New task semantics need declared attributes/checks and tests, not arbitrary unvalidated fields.

## 2. One graph, with a feature overview and linked specifications

**Comment:** `b841f96533aa4b059844a08798c74dac`, anchored to section 6's former equation of a product commitment with a tasks-app issue. Danny asks why another entity is needed and requests planned/implemented feature visibility for frontend and marketing alongside the existing backend spec-to-node linkage.

**Disposition:** accepted. A commitment now describes intended work represented by a canonical node/subgraph; it is not a mandatory new entity or duplicate task. Sections 6 and 15 define a feature/capability catalog derived from the same graph. It links spec headings, implementation nodes, checks, artifacts and release receipts, with backend/frontend/market-facing facets. Planned, specification-ready, implementation-verified, released and marketing/documentation-ready are distinct dimensions.

**Preserved distinctions:** a request, an action attempt and a verification receipt are still different objects. A cached feature row is not another authority. A spec describes intended truth and links to the node; it does not accumulate hand-maintained Done notes. A verified feature is not automatically released or publicly marketable. The proposed pilot exercises one frontend feature and one internal/backend capability, without publishing either.

## 3. VO for internal contracts and durable evolution

**Comment:** `cf4b624703c6419286644707b27c1061`, anchored to section 7's previous broad acceptance of CLI/HTTP JSON. Danny asks to avoid more ad-hoc formats and use VO's protocol evolution and long-term storage design.

**Disposition:** accepted. Sections 7, 12, 18 and 20 require new internal pilot protocols and durable domain records to reuse the owning VO schema/codec/evolution path. Third-party JSON remains at the provider boundary; debug/CLI renderings and existing export compatibility do not become another authoritative internal log. Unsupported language bindings must be implemented at their owner or accessed through an already supported generated client boundary.

**Implementation qualification:** `specs/containium/09-wire-format.md` explicitly lists incomplete client/Python/gateway paths, while `specs/vo/03-schema-identity.md` describes the intended evolution discipline. This is not proof that all required bindings are already shipped. P0 now checks actual codec availability and old/new stored-data conformance. Missing support is an owned upstream gap, not permission to invent another JSON protocol. A format cannot automatically make an incompatible semantic change backward compatible.

## 4. TypeSafe and the meaning of decisions

The new research answers Danny's suggestion affirmatively at the integration level: a typed probabilistic evaluator is useful inside an AxE-managed decision workflow. Its hosted evaluation is an Op, and deterministic code consumes the recorded result. It is not a drop-in AxE Decision, permission grant or Telos planning decision. Section 8.2 adds the optional shadow lane and its response validation, privacy, model pinning, retry, stale-result and replay contracts. Pure coordinators remain the pilot default; AxE generator modes are also acknowledged rather than being excluded by an over-broad statement.

## 5. Delivery and review state

The export reports that the original automatic delivery failed because there was no live tmux window/session named `rig`. The remarks themselves survived in the spec-web store. This pass reads that durable state explicitly and replies through `review spec-web reply --no-tg`; no Telegram message or dummy agent session is needed. Reply receipt verification is recorded in the companion evidence record after the commands run.

Changes in this document are specification changes, not deployed features. A substantive architectural review of the revised ownership/VO/model boundaries and document-specific publication checks are recorded in the evidence record. The three comments must remain available with their original IDs and history; their reviewer-resolved status is not changed by this response.
