# PhD context

Stable reference. Update on major pivots, not weekly. Sections are independently
loadable — paste only what's relevant when re-entering a chat.

## §1 Thesis

**Problem.** IaC encodes static decisions about resources whose runtime behavior
is dynamic. There is no systematic way to translate observed runtime evidence (System metrics, and application metrics)
back into the specific IaC construct that should change.

**Approach.** A typed mapping model `runtime observation × IaC context → patch`,
instantiated as a recommender that emits source-code patches with rationales,
evaluated on a benchmark of misconfiguration scenarios across multiple apps.

**Differentiation.** Existing work optimizes config *values* (CherryPick,
OtterTune) or generates IaC code without runtime grounding (Lightspeed). Ours
operates on versioned source artifacts with traceable evidence-to-patch links,
across the four IaC layers.

**Year.** End of Y2; Y3 starts Sept 2026. Two papers targeted in Y3.

## §2 IaC layer model

| Layer | Tools | Knobs | Monitoring scope |
|---|---|---|---|
| L1 image | Dockerfile, JVM opts | heap, GC mode, base image, libs | app / process / container |
| L2 orchestration | K8s, Helm | requests, limits, replicas, affinity | pod / container / node |
| L3 config mgmt | Ansible, env files | DB pool, worker procs, cache, timeouts | service / cluster |
| L4 provisioning | Terraform, EnOSlib | instance type, count, network, region | node / VM / host |

**Bidirectional reading.** Top-down: a layer determines its monitoring scope.
Bottom-up: a monitoring scope reveals which layer might need to change. Single-layer
recommenders fail at bottom-up disambiguation when symptoms manifest above
the actual layer (e.g, through monitoring a pod, we can either say that the app deployed on it (the container) isn't well configured or it's resource cosuming(hence a recommendation at L1), or we can say (a level above) the node on which the pod is deployed should have more resources (hence a recommendation at L4)).

## §3 Potential Paper plan

**Paper 1 — recommender.** Single-layer (L4 provisioning first) recommender
that produces source-code patches. Mapping model formalization + technique +
benchmark + evaluation with SE-quality metrics. Target: ICSE 2027 NIER (4pp,
~Sept 2026 deadline) as stake-in-ground; full version at FSE 2027 / ICSE 2028.

**Paper 2 — cross-layer.** Graph representation of multi-layer IaC project +
localization: given a symptom, identify which layer's artifact should change.
Empirical demonstration that single-layer recommenders frequently mis-localize.
Target: ICSE 2028 / FSE 2027.

**Mapping model.** First-class artifact, not a separate paper. Formalized in
paper 1, generalized in paper 2. Possible journal version (TOSEM/EMSE) later.

**Benchmark.** Reusable artifact, possible Tools/Datasets-track double-dip
alongside paper 1.

**Dropped.** Empirical study of IaC-runtime feedback in practice (already
covered by my prior SLR on IaC QA).

## §4 Experimental harness
**Current** Just validate and develope the solutions locally on my laptop, before going to a bigger scale

**For future scale Testbed.** Grid'5000 via EnOSlib. Medium budget (dozens of node-hours/week).
External-validity validation pass on AWS late in paper 1's lifecycle.

**App.** CorrectExam (JHipster + Quarkus + MariaDB + MinIO + Angular).
3-tier with binary-blob storage, repos at github.com/correctexam.

**Stages.**
- S0: deploy locally, run the different load tests and scenarios, build a first prototype (using docker-compose for now). The idea is to test all the different stages locally before moving to grid5000
- S1: single node, docker-compose. Built. L1 + degenerate L4. (in grid5000)
- S2: 3 bare nodes, EnOSlib/Terraform(maybe with packer, if it's just about VM creation) + Ansible. Planned. L1 + L3 + L4. (in grid5000)
- S3: k3s cluster. Planned. All four layers. (in grid5000)

**Workloads.** W1 browse · W2 upload · W3 grade · W4 mixed. Drives different
tier pressures so each experiment's perturbation lands on a stressed tier.

**Hardware variation.** cgroup caps for controlled "instance size" simulation;
real Grid'5000 cluster heterogeneity for external validity.

**Per-run labels (mandatory).** Every Prometheus series and Locust CSV carries
`experiment_id`, `scenario`, `iac_sha`. The `iac_sha` ties runtime observations
to the exact IaC artifact version that produced them.

## §5 Related work — cluster summary

Read-list lives in `RELATED_WORK.md` (separate). Cluster pitches:

- **Cloud config auto-tuning** (CherryPick, PARIS, Ernest, Selecta, Arrow,
  Scout). They output values; we output source patches with rationale.
- **DB/system tuning** (OtterTune, CDBTune, ResTune, LlamaTune, BestConfig).
  They tune live values; we patch source.
- **IaC quality / SE** (Rahman, GLITCH, Sokolowski, Sharma). Static analysis;
  we are dynamic-evidence-driven.
- **LLM for IaC** (Ansible Lightspeed, FORGE workshop papers). Code completion;
  we ground in runtime evidence.
- **K8s autoscaling / placement** (Autopilot, Sinan, FIRM, Madu). Live control;
  we generate developer-facing source patches.

## §6 Constraints

- ICSE 2027 main track (Jun 2026 deadline) is **not** a target — too compressed.
- Cost optimization is a tracked secondary objective, never the headline.
- Single-layer recommender first; cross-layer is the ultimate goal, but we can start with just one layer as a first step.
- I am not pursuing self-healing / closed-loop control in this PhD.

## §7 Glossary

- **Cell** — one (scenario, workload, deployment) tuple in an experiment matrix.
- **Scenario** — a specific IaC configuration, baseline or misconfigured.
- **iac_sha** — git SHA of the IaC artifacts used for a run.
- **Mapping model** — typed relation
  `(signal, value, window, target) × (layer, file, construct, value, location) → {(patch, rationale, confidence, scope)}`.

## §8 General ideas to keep in mind
The goal is to close the feedback loop from Ops to Dev, in order to enhance IaC scripts.
We want to have a sort of context awareness (especially the dynamic side of IaC scripts). 
By monitoring the Ops/Dynamic metrics (throughput, cpu_95, memory_95, latency, logs, traces, number of requests, ...) we want to provide informed IaC changes/patches for better performance.