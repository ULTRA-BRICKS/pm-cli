# Interleaved Semantic Review in RIG + AxE

**Status:** research and integration proposal, not implemented inference. **Reviewed:** 2026-09-18. **Tracking:** pm-cli #30. **Parent:** `rig-axe-pilot-master-spec.md`, sections 8.2–8.4 and 15.3. This expands the shadow workload beyond intake; native-runtime, authority and privacy gates remain unchanged.

## 1. The useful change of emphasis

An inexpensive evaluator can inspect a bounded artifact between expensive coding-model steps. A final-only reviewer may find a bad assumption after dependent changes have accumulated; intermediate review could surface it earlier. This is a shorter-feedback-loop hypothesis, not an observed speedup. Ask concrete questions against the current patch and relevant requirements rather than request a general opinion about the repository.

The loop is: record a stable patch; run applicable deterministic checks; request semantic evaluation through an AxE Op; admit a validated basis-bound receipt; apply deterministic policy; optionally feed one finding to the next bounded coding step; independently verify the final artifact. Pure decide never calls the provider directly. A generator-mode agent may yield the same recorded Op under that mode's retention/replay contract.

A model judgment, an AxE effect decision and a Telos planning decision are different concepts. A correctly shaped or confident answer supplies no execution permission. A plausible answer may still be inapplicable because the patch or requirement has changed.

## 2. Linked demonstrations and their limits

### TypeSafe launch and game control

The vendor launch motivates typed low-latency judgments and a game controller consuming structured state, not image input. Its selected workflows and model-based reference probabilities are not an independent code-review benchmark. The vendor itself documents advantageous short-context conditions and limitations of its comparisons. Do not import headline speed/cost ratios into pilot acceptance. Source: [TypeSafe launch](https://typesafe.ai/blog/introducing-system-one-models-and-jev).

### The Every writing experiment

Mike Taylor reports 37 documents with 21 questions each: 777 judgments in under 0.7 seconds with a very small estimated charge. This demonstrates batching small questions over text, not code-review quality. The article explicitly leaves thorough accuracy testing open. A separate small seeded-error exercise used 12 passages and four checks, with seven planted problems: Jev detected six versus seven for the reported comparator. A missed unintelligible sentence remained missed on repeated attempts. Preserve the miss and sample size instead of only the latency ratio.

The transferable pattern is bounded repeated inspection of concrete artifacts, proposed tool inputs and relevant context. A stylistic rubric is not an authorship detector; a binary warning is not an explanation or executable test. The supplied six-page PDF was read in full and is not republished with the website. [Original article](https://every.to/also-true-for-humans/mini-vibe-check-typesafe-s-jev-judged-everything-i-ve-written-in-0-7-seconds).

### Local Qwen constrained decoding

The author's model card names Qwen2.5-1.5B-Instruct and describes parallel constrained decoding, shared prefix/KV work, bounded enums/booleans and programmatic JSON assembly. The repository name says `1B-RLCD`, while the card identifies a 1.5B Instruct base: both the size label and the training implication must be checked against the actual artifacts. The inspected card establishes an inference implementation, not new RLCD-trained weights or Jev equivalence. Stronger training claims require training code, dataset, checkpoint and evaluation provenance.

The card calls normalized softmax scores calibrated. The formula establishes normalization, not empirical calibration on held-out tasks. Its M4 Max latency and memory table is author-reported, not reproduced here. Compare equal context sizes, multi-token candidates, quantization, cold/warm state and error criteria. Separate MLX and Spaces/PyTorch packaging paths are present; neither was installed or benchmarked in this research. [Model card](https://huggingface.co/harshatheg/Qwen-2.5-1B-RLCD/blob/main/MODEL_CARD.md), [source tree](https://huggingface.co/harshatheg/Qwen-2.5-1B-RLCD/tree/main), inspected short revision `2af8684`.

### Use cases and non-requirements

The official use-case map includes semantic code linting, harness engineering, retrieval, verification and lead generation. The first four motivate bounded experiments here. Lead generation illustrates classification/routing; it does not authorize a sales agent or outreach. The linked comedy clip contributes no technical evidence or acceptance requirement. [Official use-case map](https://docs.typesafe.ai/concepts/use-case-map).

## 3. Four separate proofs

| Claim | Required evidence | Does not establish |
|---|---|---|
| Valid shape | Generated schema and request-dependent validation | Correct interpretation. |
| Useful semantic accuracy | Held-out labels by defect class, language and context | Calibration or permission. |
| Empirical calibration | Predicted probabilities versus observed frequencies | Certainty about an individual case. |
| Action authority | Current authenticated principal, scope, policy, basis and lease | Granted by no model score. |

Keep Choice distributions, Noul proposition probabilities and Telos evidence confidence separately named. Low Noul can strongly mean no, not uncertainty. Batched questions may have correlated errors; their probabilities cannot be multiplied as though independence were established.

## 4. Qualification sequence

**E0 — Interface:** synthetic intake/matching tests for schema, identities and real-runtime admission/replay, not model-quality claims.

**E1 — Shadow code pre-review:** fixed small patches plus requirement clauses. Include clean code, missing failure paths, scope drift, duplicate authority, misleading comments, missing context and unknown cases. Compare deterministic checks, a permitted general reviewer, authorized Jev and a qualified local baseline on the same corpus. Record observations without blocking work or changing instructions.

**E2 — Bounded advice:** only after E1 satisfies a versioned operator-approved error/coverage criterion. Deliver one deduplicated revision-bound finding before the next expensive step. Measure useful corrections against latency and false interruptions. Independent final gates remain unchanged; no automatic merge, deployment, risk acceptance or pursuit closure.

**E3 — Optional training:** only for a measured gap with approved data rights and resources. Keep repository/request/patch families together across train/development/held-out splits. Preserve expert-reviewed labels, negatives and unknowns; incumbent model outputs are not ground truth. Freeze thresholds before held-out evaluation and report defect/language recall, false positives, abstention, calibration and full-path resources. Never repeat evaluation until a preferred answer appears.

## 5. Failure and concurrency

Evaluation identity pins canonical node, attempt, artifact/requirement/context basis, question version and model. A new artifact supersedes old pending work. Old responses remain historical and cannot affect current execution. Admitted receipt replay makes no extra provider call or duplicated feedback; loss before response admission may still repeat provider computation or cost, so no universal exactly-once inference claim is made.

Rate limit, overload, timeout, invalid shape, unknown and stale remain explicit. Coalesce equivalent requests and bound concurrency, input volume, total calls and feedback/fix cycles. AxE owns retry policy; disable overlapping SDK retries. An unavailable optional evaluator is not green and cannot suppress a mandatory final verifier.

## 6. Topology, access and site

The core remains all-in Linux/ARM64 with native AxE/Containium qualification. An approved Mac/MLX experiment may inform a decision but does not satisfy that gate. A collaborator reporting account access is not a credential delegation or permission to transmit private source. No provider account, paid inference, private-code transmission, model download or training occurred in this update.

The EN/RU site demonstrates these contracts with deterministic synthetic browser state. It is neither a deployed coordinator nor a measured Jev demo. Public-ready content includes explanations and public references, not Slack transcripts, gift-token links, account details or article PDFs. English remains normative; Russian is explanatory.

## 7. Retrieval coverage

The provided Slack thread was read through its last returned reply: parent plus nine replies. Four linked X post texts and metadata were recovered from X's official public syndication surface after ordinary browser navigation failed. Two embedded long-form texts were truncated; the full vendor article and local model card were read as primary corroboration. Videos were not exhaustively watched/transcribed and supply no accuracy proof.

The Every PDF was read in full; TypeSafe use-case documentation and the Qwen model card/tree were opened in a real browser. A cold Hero source invocation failed with a missing `@axe.sc/hero/challenge` module. That is an observed local startup failure, not a general claim about Hero's capability. Source descriptions and author-reported numbers are not independently reproduced benchmarks. Implementation promotion remains gated by the actual workload experiment.
