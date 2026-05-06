# Papers

One section per paper. Update when the structure firms up. Each section is
self-contained — you can paste a single paper's section into a chat to discuss
it without loading the others.
That's just a first version of papers' thoughts, and the list is succeptible to change/enhancement/improvement.

---

## P1 — Evidence-grounded provisioning patches

**Status.** Active. NIER stake-in-ground at ICSE 2027 (~Sept 2026); full
version at FSE 2027 / ICSE 2028.

**One-line pitch.** Given runtime telemetry and an IaC repo, produce a
minimal, rationale-annotated source-code patch at the provisioning layer.

**Contributions.**
- C1 — formalized mapping model (typed relation, one-layer instantiation)
- C2 — recommender technique (rule candidates + LLM rationale + minimality)
- C3 — misconfiguration benchmark (~20 scenarios, ground-truth fixes)
- C4 — evaluation: accuracy + SE-quality + small developer study

**Baselines.**
- B1 — CherryPick-style Bayesian optimization over config space
- B2 — Naive LLM (Lightspeed-style code completion w/ metrics in prompt)

**Required experiments.**
- E1 JVM heap × W2 — L1 perturbation, reduced version built
- E2 MySQL buffer × W3 — L3 perturbation
- E3 cgroup CPU × W4 — L4 perturbation, the headline experiment
- E4 N apps × baseline — generalization (TeaStore, Sock Shop, etc.)
- E5 dev study — patch utility ratings (~10–15 engineers)

**Open issues.**
- LLM-only baseline may match accuracy → must win on SE-quality. Validate early.
- Layer-correctness metric needs precise definition before E1 analysis.
- Dev study recruitment is on critical path; start ~3 months before deadline.

**Section map (NIER 4pp).** Intro · Mapping model · Technique · E1+E3 results
· Comparison to B2 · Roadmap to full version.

**Section map (full).** Intro · Related work · Mapping model · Technique
· Benchmark · Evaluation (accuracy / SE-quality / dev study) · Threats · RW · Concl.

---

## P2 — Cross-layer IaC reasoning

**Status.** Planned. Year-3 second half. Target ICSE 2028 / FSE 2027.

**One-line pitch.** Multi-layer IaC project as a typed graph; localize
which layer's artifact should change given a symptom; show that single-layer
recommenders mis-localize on a measurable fraction of cases.

**Contributions.**
- C1 — graph representation spanning L1–L4
- C2 — localization algorithm (symptom → layer)
- C3 — empirical: single-layer mis-localization rate
- C4 — cross-layer recommendations

**Required experiments.**
- E6 cross-layer trap — same symptom, fixes at different layers
- E7 graph-based vs single-layer recommender comparison
- (reuses P1's benchmark and harness)

**Dependencies.** P1's mapping model and benchmark must exist first.

---

## P0 — Mapping model formalization (latent)

**Status.** Embedded in P1, not standalone. Possible journal extraction
later (TOSEM/EMSE) if depth warrants it. Don't pursue separately.

---

## Dropped

- **Empirical study of IaC-runtime feedback in practice.** Already covered
  by prior SLR on IaC QA.
- **Self-healing / closed-loop control.** Out of scope for the moment, but can integrated later if we got the required results.
