# TypeSafe / Jev in the RIG + AxE Decision Loop

**Research date:** 2026-09-18. **Status:** source-backed integration proposal; no paid inference or production integration executed. **Tracking:** [pm-cli #30](https://git.hyperide.ai/ultrabricks/pm-cli/issues/30). **Parent:** [RIG + AxE master specification](https://spec-dev.hyperide.ai/spec/rig-axe-pilot-master-spec). Danny's question is whether a model that emits decisions fits inside an AxE decision function.

## 1. Conclusion

Yes: Jev is a candidate semantic evaluator inside the AxE-controlled workflow, not a replacement for its runtime decision function. The model estimates a bounded answer from supplied context; deterministic application policy chooses which permitted AxE effect to emit. A `Choice` answer is data, not an authenticated command. Use an AxE Op for the remote evaluation and admit its result before policy consumes it. This separation is precisely where typed probabilistic output is useful.

The qualification starts with shadow intake classification and requirement-to-node matching, then adds a distinct interleaved semantic code pre-review workload between bounded coding-model steps (master sections 8.3–8.4). Do not ask Jev to reconstruct Telos's dependency graph, calculate the frontier, approve a plan, grant a lease, mark tests passed or declare a release. Those are existing deterministic or evidence/authority-owned decisions. A high model probability never overrides their contracts.

## 2. What the public interface actually provides

TypeSafe describes Jev as a System One model trained for calibrated, machine-consumable judgments rather than free-form generation. The current documented input is text, including structured objects and arrays containing text; it is not an image/audio/video model. It does not produce code, explanatory prose or a full implementation plan. Those boundaries are explicit in the [System One documentation](https://docs.typesafe.ai/concepts/system-one).

The [primitives](https://docs.typesafe.ai/primitives) and [API reference](https://docs.typesafe.ai/api) define three answer families:

| Primitive | Meaning | Response shape |
|---|---|---|
| `Choice` | Select from caller-defined alternatives. | Selected choice, probability distribution and confidence statistic. |
| `Noul` | Estimate the probability that a specified proposition is true. | `noul` in [0,1]; no separate confidence field. |
| `Score` | Evaluate ordered rubric levels. | Probability-weighted level, legend, distribution and confidence statistic. |

Questions sharing one input state can be evaluated together. Each question must include its full semantic instructions; the question ID is a response key, not model context. A later judgment needing newly retrieved evidence or a prior answer requires another stage. Evaluation independence is not statistical independence of errors; multiplying separate probabilities does not automatically produce a valid joint probability. This last constraint is our integration requirement, not a vendor guarantee.

The live HTTP contract is `POST https://api.typesafe.ai/v1/systemone` with bearer authentication and JSON containing `state`, `model` and named `questions`. The official JavaScript package is `@typesafe-ai/sdk`; its [README](https://github.com/typesafe-ai/typesafe-sdk-js) uses `TypeSafeClient`, `choice(...)` and `client.systemOne(...)`. This is a real documented API and SDK, not a guessed package or an OpenAI chat-completions wrapper.

## 3. Three meanings of a decision

Keep three separate concepts. A TypeSafe result is a probabilistic interpretation. An AxE `Decision` is a declared effect/wait/termination instruction built through the agent's typed context. A Telos `decision` node is a planning divergence whose resolution may require evidence and human authority. Sharing an English word does not make their contracts interchangeable. Model output cannot fabricate `authorRole`, a lease grant, a passed check or a human answer.

The pilot selects pure coordinator functions. In that mode a fresh invocation reads admitted state and deterministic context, returns an evaluation Op when needed, and returns an allowed follow-up only after the Op result has become state. The SDK request runs in the I/O-capable Op implementation. It must not call `fetch`, await `client.systemOne(...)`, sleep or load secrets inside the hermetic pure decide realm.

AxE also specifies cooperative and non-cooperative generator modes. A generator may yield a typed evaluation Op and use the returned recorded result; the runtime, not an arbitrary Promise, owns suspension and replay. Thus a small model can be used naturally *within decision logic* without changing the runtime's I/O boundary. The pilot's pure choice is not a statement that AxE supports only synchronous workflows. Source references are `specs/axe/11-decision-function.md`, `11a-decision-function-sync.md` and the archived `11c-decision-function-async.md` at AxE revision `0f3e99779206159ee0aa3601ee4e9bc010bf53eb`.

### Proposed flow, not an existing adapter

`admitted request → bounded semantic-evaluation Op → hosted Jev response → runtime validation → generated VO-backed evaluation receipt → reduced state → deterministic policy → permitted AxE effects`

For example, "Undo erases my manual edits" can be classified as a defect report and matched against supplied candidate requirement nodes. An explicit `none/uncertain` alternative prevents forcing an unrelated match. The matching proposal retains its probability distribution and provenance. Telos remains responsible for what the candidate blocks/unlocks, whether work is ready and how it ranks; executable code, tests, authorization and release checks remain outside the model's remit.

## 4. Confidence is not evidence or permission

The vendor's [confidence documentation](https://docs.typesafe.ai/confidence) distinguishes outcome probability from a summary of distribution concentration. `Noul` is the probability of the proposition, not a separate confidence score: a value near zero can strongly favor **no**, while a value near the middle is ambiguous. Choice and Score provide a separate confidence statistic. None is Telos's evidence-confidence predicate, a check result or proof that a particular answer is correct.

Our gate calibrates thresholds per task against held-out labels and the error cost. It never imports the vendor's illustrative threshold as an authorization rule. A reject/defer outcome remains possible even for valid, high-confidence output when the input basis is stale, an action is forbidden, or the candidate is not in the authorized set. A label such as `approval_response` may identify message intent; only the authenticated approval protocol can turn that message into authority.

## 5. Limits and implementation traps

The [Jev limitations page](https://docs.typesafe.ai/model-jaggedness/jev-1.13), reviewed by its authors on September 17, identifies weaknesses in arithmetic/counting/date ordering, literal or indirect instructions, irrelevant context and adversarial input. It also warns that separately phrased questions need not obey the logical probability relationships a caller assumes. Our design therefore computes dates, totals, dependencies and structural identities in code and tests adversarial examples rather than assuming typed output eliminates semantic error.

The [model registry](https://docs.typesafe.ai/models) currently lists `jev-1.13.0`; moving aliases are not reproducibility pins. The evaluation record names the requested version and returned model identity. English is the documented primary language, so Russian and mixed-language requests in this ecosystem require their own evaluation set. No local/offline Jev deployment has been established by this research.

Reading the official [JavaScript client implementation](https://github.com/typesafe-ai/typesafe-sdk-js/blob/main/src/client.ts) revealed three integration-relevant details: the parsed response is returned with a TypeScript cast rather than response-schema proof; retries are implemented in the client; and debug logging includes request/response bodies. These are source observations, not reproduced exploits. The Op adapter must validate the response against the requested question types/options, disable body logging and avoid multiplying SDK retries by AxE retries. The inspected configuration uses `retry.maxRetries` and `timeout`; examples must not invent `timeoutMs` or an unsupported provider idempotency guarantee.

Our adapter uses generated boundary validation plus request-dependent invariants: required answer IDs, exact primitive kind, valid candidate keys, finite/ranged probabilities, normalized sums within a documented tolerance, valid legend/score relationships and the expected pinned model identity. A failure is a typed invalid-provider-response result, not a successful verdict. Optional provider metadata may be preserved separately; it cannot become authority. The internal accepted receipt is generated from the owning VO schema, not an ad-hoc JSON file beside the event log.

## 6. Durable evaluation contract

The proposed evaluation identity binds workspace and input provenance, relevant graph/specification basis, authorized candidate set, state digest, question/rubric version and pinned model. Each permitted provider attempt has a separate attempt identity and budget receipt. Record the actual resolved model, distributions, response-validation outcome, usage and timing. The policy version consuming an answer is recorded separately from the inference question: changing a threshold does not silently change the historical answer or retrospectively authorize an effect.

A model result is admitted only for its still-valid request basis. A changed requirement, withdrawn request or revoked scope makes a late answer historical evidence, not current dispatch input. Cached answers are reused only under the same relevant input/schema/model basis and access scope. A deterministic recorded fixture tests the adapter protocol but is never described as a measured Jev prediction.

Known admitted results survive restart without another model call. A crash after the provider computed but before its response was durably admitted may still repeat inference or billing; AxE cannot manufacture exactly-once execution inside a third-party model service. Unknown outcomes consume recorded attempt budget and require the configured bounded retry policy. Never keep asking until a preferred or more confident answer appears. One accepted evaluation cannot dispatch twice after replay.

Set SDK automatic retries to zero when AxE owns retry scheduling; set a finite per-attempt timeout and cancellation signal. Handle authentication, invalid requests, rate limits, overload and permanent failure as separate outcomes. The total call/elapsed budget includes failures and discarded stale results. An unavailable model yields a visible abstention or an already authorized alternative, never a silent privacy-crossing fallback. The deterministic core pilot must operate without a TypeSafe account.

## 7. Privacy and deployment

Use only synthetic/public fixtures until the operator explicitly allows a hosted route and its data scope. The documented service is a remote API; installing its SDK does not put the model inside the pilot container. An approved Op service owns the key, and neither browser nor coding worker receives it. Send the minimum relevant text, not the complete repository, all Telegram history or raw secret-bearing logs.

The [legal page](https://docs.typesafe.ai/legal) distinguishes available enterprise zero-data-retention arrangements from a universal default. A no-training statement is not a no-retention promise. Verify the actual account terms, retention, access and region requirements before private data is sent. No new provider credential was read, no account was provisioned, and no live inference request was made in this research.

## 8. Qualification experiment

Start with shadow-only classification and candidate matching on labeled English, Russian and mixed-language fixtures. Include negation, indirect requests, edits, missing context, misleading tool output, unrelated candidates, prompt injection, conflicting evidence and an explicit abstain class. Split by originating request/thread, not random revisions of the same message, to prevent evaluation leakage. Human-reviewed labels are the reference; another model's agreement alone is not ground truth.

Compare deterministic parsing, the existing permitted general-model path and Jev on the same scope. Measure classification/matching error at a fixed permitted automation coverage, abstention rate, per-class errors, probability calibration, latency distribution, billed usage where observable, and complete orchestration overhead. Report failures and confidence intervals rather than only averages. Tune thresholds on a development split, freeze them, and report the held-out result. Do not calculate an exact physical quantity by interpolating a Score rubric.

Additional protocol tests cover malformed-but-JSON responses, missing answer IDs, unknown options, invalid distributions, request edits during inference, duplicate delivery, crash before/after response admission, SDK/runtime retry multiplication, timeout/cancellation, quota exhaustion, unauthorized source inclusion and secret-bearing debug logs. Pure AxE and any optional generator lane must be tested on the real runtime with their own retention semantics.

The vendor's [launch benchmarks](https://typesafe.ai/blog/introducing-system-one-models-and-jev) motivate investigation, not an expected speedup for this system. Their schema-validity claims do not prove that the chosen option is semantically right. This note deliberately claims neither lower measured pilot latency/cost nor production reliability. Promotion from shadow to low-risk assistance requires the local experiment; authority, arithmetic and evidence gates stay deterministic regardless of its result.

## 9. Interleaved review and local baseline follow-through

The [interleaved-review research](./rig-axe-interleaved-review-research.md) expands the evaluation beyond intake. It analyzes the Every writing experiment, the typed game-control demonstration and the local Qwen constrained-decoding implementation. None proves our code-review quality or latency. Intermediate findings are revision-bound advice, not final acceptance, authority or another planner. A newer patch invalidates the old evaluation's applicability; repeated receipts cannot deliver feedback twice. Unknown/timeouts stay visible and mandatory final checks remain independent.

The local example's model card names Qwen2.5-1.5B-Instruct and an inference technique; its repository name does not establish newly RLCD-trained weights. Evaluate the unchanged baseline before optional fine-tuning, separate syntax validity from semantic correctness and empirical calibration, and preserve held-out patch-family/language splits. Mac/MLX results are not Linux pilot qualification. No new model was downloaded, trained or invoked in this update.
