# EMNETS-RA research engine

Current deployment (2026-10-29): all model roles use **Qwen3.8-27B-FP8** on the configured private OpenAI-compatible service. The private launcher environment enables a provider lock covering explicit model overrides, old project configurations and native tool calls. Research, coding, image inspection, chat, review, idea judging and independent judging share this model; fallback providers are disabled. Credentials are loaded from `~/.config/emnets-ra/provider.env` (0600), never from a committed key. Text JSON, vision and native function calls have been probed successfully. Historical Flash benchmark records below retain their original models and protocols.

The product now includes a skill-library route (`#/skills`) with provenance and no-model routing previews; per-run skill records retain exact revisions, hashes and loaded references.

The shipped project configuration enables `research_engine` with the `quality_first` profile. The priority is task fidelity, executed evidence, reproducible comparisons, and defensible conclusions. Token and time reductions require a separate measured comparison.

V33 is frozen at source `757c6bb8f2da9da6f7651b4e940ebb784ee883a2700234f167bbbe67a60b9d67`, with 496 relevant checks passed. V34 is frozen with 539 relevant checks passed at source `5c630a19d3290acf99e2e8377c7513fa034867138d67d4332ea789b152902925`. Both use settings `15a7eaea49d5296c8ac8477ece6e3eec91ffaec886613f9a57bb88e0414ccaef`. Original public HTTP responses have a hash-verified shared cache; research conclusions and benchmark feedback never enter it. Method-source IDs resolve to exact original quotations before strict validation. An incomplete held-pilot timeout can retry once with unchanged source/oracle inside the original shared runtime; completed assertion failures do not receive this retry.

V34 prefers original PMC JATS XML and retains verbatim LaTeX/MathML, formula labels, raw XML and response hashes. Complete formula blocks remain selectable in the source-anchor catalog. BioC fallback explicitly leaves formula coverage uncertified, and graphic-only equations remain unextracted gaps. A real public-source probe preserved all 12 original TeX expressions, recovered the required formula span and replayed the identical original response offline; this is engineering evidence, not a scientific finding or benchmark score. Read-only historical snapshot audits and independent reassessment before restoring old scientific conclusions continue to apply.

The private deployment enables `EMNETS_UNLIMITED_TOKENS=1` and `EMNETS_QUALITY_FIRST=1`. Every research role and retry uses Qwen3.8 xhigh with native thinking sampling (temperature 1.0, top_p .95, top_k 20). Output and thinking caps are removed from actual requests, including template quotas; there is no call-count quota. Healthy streaming generation may exceed 600 seconds; the timeout instead detects 600 seconds without semantic progress, and empty keepalives cannot extend it. Socket timeouts and bounded repairs remain. The service still has its own 262,144-token total context capacity. Internal acceptance targets 9/10 with at most eight evidence-bearing repair rounds; this target is not an expected external benchmark score. Scientific source budgets remain 512K characters, or 768K for formal work. Official independent grading keeps its original rubric, 500-token judgment budget and disabled thinking, using the same Qwen model.

V30 ended without official scores: Physics failed the copied-receipt audit before repair, and Math failed a source quotation. Live V31 and V32 attempts retain their own frozen code and settings. A preselected V34 three-task suite is queued behind the earlier V32 Math queue, sharing the native admission limit and 30-second read-only monitoring. As of 2026-10-29 05:44 UTC, Math and Earth have completed real execution in V31; V32 Physics is revising scientific gaps after its first report, Earth is executing, and Math is queued. Three-task export and grading are incomplete. Queue and campaign metadata are checked every 30 seconds; run health is written every 10 seconds. V23's historical Math result was 34.9/100 under the former provider. See [versioned validation, diagnostics and native-run records](researchclawbench.md).

The current telemetry counts Qwen's `delta.reasoning` stream as progress, alongside `delta.reasoning_content`; no reasoning text is persisted or included in the final answer. Regression checks inspect progress during reasoning before visible content arrives. The terminal V24 candidate retains its original frozen source and excludes this later telemetry fix; V25 includes it.

Historical configs without an explicit cap retain the class default of 48. V16 passed 541 relevant checks in the candidate and main project; v17 passed 86, v18 passed 186, v19 passed 166 and v20 passed 150 checks covering their changed behavior. These are not benchmark scores.

Instruction extraction uses controller-assigned fragments from the raw task and binds their exact quotations to its SHA256, independently of method/skill suggestions. Planning and review retain per-requirement evidence routes, mechanism fidelity and artifact completeness; a context-only or partial result cannot pass solely because the model labels it met. Source budgets follow the active profile described above, and every packet records actual coverage and text hashes. PMC supplement hits can recover canonical main-article text through the public NCBI BioC API within the existing fetch budget, retaining the raw response and passage metadata. Unavailable main text is never represented by a review-letter supplement.

All focal method texts are considered together: when they fit with identified short background excerpts, the packet supplies their complete cached text. Otherwise they share the weighted budget. Fixed background reservations cannot force a nearly fitting focal source down to half coverage. Raw text remains unchanged; Markdown-escaped author-code URLs are recognized separately and shown as links whose code has not been fetched.

Before starting another scientific repair, the controller checks the target phase's actual remaining provider allocation. When unavailable, it records the stop and retains the just-completed failed assessment and unchanged evidence. It does not invalidate completed phases or alter the task contract first. Eligible reviewed partial deliveries can use the existing strict export audit; unresolved science remains explicit. Before scientific repair, the short workflow now snapshots the complete reviewed evidence. Later candidates are compared under the same task, contract and input identity. Loss of supported requirements or passing dimensions, or lower quality at equal requirement coverage, retains the prior bundle through a recoverable multi-file transaction. Budgets, attempts and rejected evidence are retained. A historical review remains historical, and scientific gaps remain explicit.

Python revisions can use hash-bound `symbol_edits` for named functions, classes or assignments. The controller resolves original AST boundaries and retains visibility, overlap, size, syntax and atomic-publication checks. Exhausted provider retries are not multiplied by the phase controller. Native `rcbench agent --resume` is restricted to unchanged, unscored transport failures: source, settings, instructions and inputs must match, the original model-call budget persists, and resume history marks latency as noncomparable. It cannot resume an old source version with the new implementation.

## Six-phase execution

The shipped configs now set `research_engine.workflow: compact`: ground → plan → implement → execute → report → verify. This controller bypasses the legacy 26-handler cascade. It retains the original task, task-specific acceptance criteria, skill routing, real execution receipts, independent scientific review, and bounded local repairs. `compact_state.json` stores phase inputs and output hashes; resuming unchanged successful phases makes no model calls.

Normal generation returns one complete validated source bundle and one complete report. It does not open a file-by-file planning conversation. With independent design review enabled, the normal path uses six research-model requests; successful execution itself uses no model request. A concrete execution failure triggers one scoped source/configuration patch, then the controller executes the full program again. The compact workflow no longer opens an agent inspection loop for runtime repairs. The final scientific review receives actual plots (up to four per request; included/omitted images are recorded). Official benchmark grading calls are additional and follow the unchanged official scorer.

Revisions now return scoped original-text edits with complete file hashes. All edits are validated before publication; Python syntax, exact unique text matches and path boundaries are checked. Unchanged source and report content remains byte-for-byte intact. Journals retain before/after files, interrupted publication rolls back, and an already committed patch can be recovered without another model call. Each scientific review is archived with the hashes and snapshots of the artifacts it actually assessed. A concurrent artifact change invalidates acceptance.

The preferred revision format uses original one-based inclusive line ranges with the complete file hash. All ranges refer to the same immutable original, cannot overlap or include undisplayed lines, and are applied from the end of the file. This avoids copying long old LaTeX passages back into JSON. Legacy exact-text edits remain supported. Up to 64 local edits are allowed, with a 12,000-character original-block limit and at most 20 new source files. Report generation and review receive the actual executed output manifest and bounded artifact contents; draft promises of proof files cannot masquerade as generated evidence. Written mathematical proofs are reviewed rigorously without inventing a mandatory proof-assistant requirement.

If a later implementation/report revision is rejected before changing the reviewed artifacts, the benchmark adapter can retain the previous reviewed partial delivery. It requires a successful deterministic delivery audit, the exact latest archived scientific review, and matching hashes for every required scientific input. Original-input and framework-version checks still apply. Failed revision stages remain failed; `scientific_status=gaps` and `internal_verification=false` remain explicit. There is no such fallback for unreviewed, modified or failed-execution artifacts.

An implementation manifest owns the source bundle explicitly. Generated reports, figures and outputs do not become source merely by appearing under the execution directory. Binary source assets remain hashable and exportable but are excluded from text edits. Captured older runs use a compatibility source filter. Report image aliases and structural filename prefixes bind only to a unique real PNG from the actual execution; ambiguity remains an error.

Input profiling includes a separate bounded deep CSV catalog, grouped by original column names with source paths and raw value samples. This preserves nested input families, fractional dates and units when the displayed inventory is truncated. Planning, design review, implementation, runtime repair and scientific acceptance receive this context. The catalog distinguishes file origins; scientific code must still establish schema mappings, lineage and unit/interval semantics rather than indiscriminately concatenate derived results and raw observations.

The catalog also retains bounded adjacent README and dataset/schema definitions. Runtime repairs receive the tables and raw samples actually produced by the failed computation, plus an available schema diagnostic. This is especially useful when a program exits successfully but all method labels or primary estimates are missing.

Scientific source repairs retrieve task/gap-relevant passages from the already extracted full reference texts, retaining source offsets and bounded per-source context. Early abstract excerpts no longer limit later method, units or proof repairs. These local reads add no model requests. A narrowly defined premature-root-brace response error can also be repaired without another request: all remaining object members must parse completely, the original scientific schema still validates, raw and normalized responses are retained, and provider-truncated candidates remain rejected.

Reviewed plans may declare `execution_checks` linked to requirement IDs. JSON outputs support `finite_fields` and `positive_fields` expressed as JSON pointers. CSV/TSV outputs support `min_rows`, `min_non_null` counts by column, and exact `allowed_values` for metadata. The controller reads these outputs before reporting, archives each check result, and treats a failed precondition as an execution repair. Source patches cannot edit this plan. Checks describe necessary coverage and measurements, never expected benchmark answers or scientific acceptance. Legacy plans without these optional checks retain their prior validation. The final artifact audit repeats declared checks; independent scientific review remains required.

JSON checks also support `numeric_bounds`, mapping each JSON pointer to inclusive `min` and/or `max`. These capture justified task/method tolerances and counts, rather than merely accepting any finite number. Nulls, strings, booleans and nonfinite measurements fail; malformed and inverted bound definitions are rejected at planning. The same vocabulary is shown to the planner, design critic and plan repair. Thresholds cannot be copied from expected scientific outcomes or a benchmark answer. Complex domain assertions still belong in reviewed code and independent scientific review.

Theoretical plans include `scientific_analysis_markdown`, retained as an immutable initial analysis at `stage-09/scientific_analysis.md`. The report phase owns the current analysis at `stage-25/scientific_analysis.md`, so correcting a proof does not invalidate the executable protocol or rerun a valid experiment. Numerical code writes measurements and numerical observations; long proofs are kept outside `main.py`. Report and theory corrections use one scoped patch request followed by independent review. Changes to numerical methods, measurements or plot generation return to implementation and real execution.

`public_source_search` enables bounded source discovery with the configured official DeepSeek provider's raw Search/Fetch API. Task extraction supplies at most `max_source_queries` (default 2); the controller retains at most `max_public_sources` (default 2) primary research sources. It never reads evaluation checklists, rejects benchmark/evaluation queries, records acquisition status, and sends no other provider's credentials to DeepSeek. These HTTP requests do not add model calls. They are separately billed retrieval requests according to the provider; see its [official Search/Fetch documentation](https://api-docs.deepseek.com/guides/search/). Self-contained tasks and instructions forbidding external sources use no public retrieval.

`phase_max_iterations` bounds the remaining tool-based workflows (default 16). In the compact workflow, `max_repair_rounds` bounds runtime and scientific repair rounds; a patch may make one formatting correction, and new runtime failures have separate failure/source fingerprints so an unrelated old patch cannot be reused. Adding probes or notes alone is not accepted as a runtime repair. `max_model_calls` counts provider attempts across the entire run, including retries. Shipped deployment configs use 0 for no quota; bounded historical configurations retain their own reporting reserve (class default 48). Counts persist in `model_call_budget.json` across process restarts. These are upper bounds, not quotas. Transport failures retain completed work; invalid/truncated plan patches cannot replace a valid plan. Report-only gaps return to reporting; missing experimental evidence returns to implementation/execution. Final review rechecks the raw instruction and cannot grant completion from file existence alone. Quality scores are internal model judgments, not external benchmark results.

For the official DeepSeek Flash endpoint, `artifact_reasoning_effort: none` disables thinking when emitting source bundles and reports from the validated protocol and retained evidence. Planning, design criticism, execution diagnosis, theoretical corrections and final scientific review keep the configured base effort (`high` in shipped configs). Two real low-effort code requests exhausted about 32K tokens each without delivering complete source, so lowering effort alone did not resolve output starvation. Direct artifact emission still requires source validation, real execution and independent scientific acceptance; it is not evidence of benchmark quality. Set the artifact setting to `low`, `high` or `max` to compare allocations. The provider supports the [thinking toggle and effort controls](https://api-docs.deepseek.com/guides/thinking_mode/).

Use `python -m researchclaw run --config config.arc.yaml --output /path/to/run --resume` to resume a stable version, or `--workflow legacy` for historical 26-stage runs. Source, input or task changes require a fresh run directory. The short controller currently supports local `experiment.mode: sandbox`; remote/Docker/simulated backends and existing interactive ideation/literature-only workflows retain the legacy runner. See [implementation, inspected source revisions and fault handling](architecture-upgrade.md).

## Sources checked on 2026-10-27

The official [ResearchClawBench Pass@1 leaderboard](https://internscience.github.io/ResearchClawBench-Home/) lists the following top six agent entries. Scores are averages over 40 tasks. Different entries use different models; the ranking does not isolate framework effects.

| Entry | Score | Public material | Mechanisms adapted here |
| --- | ---: | --- | --- |
| Qiushi Engine | 38.616 | [Architecture report](https://arxiv.org/html/2609.08196v1), [public research outputs](https://github.com/Oxelra-AI); complete engine source was not found | Persistent scientific handoffs and returns to the stage responsible for an evidence gap |
| InnoClaw 2.0 | 34.890 | [InnoClaw source](https://github.com/SpectrAI-Initiative/InnoClaw) | Requirement state, specialist review, source pointers, experiment repair |
| Ligase | 34.801 | [Official product description](https://www.gradence.com.cn/products/biopharma.html); no public engine repository found | Independent methodological, statistical and reproduction perspectives; implemented here with prompts, not Ligase's proprietary trained models |
| AutoSciRub | 33.159 | [MIT source and skills](https://github.com/zjunlp/AutoSciRub) | Instruction-derived executable criteria, literature/data grounding, criterion verification, targeted revision |
| EvoLab | 32.687 | [Official documentation](https://evo-lab.org/document/); the linked `CompLifeLab-ZJU/OpenEvo` repository returned 404 | Reuse scoped experience from completed research; retain and compare attempts |
| InnoClaw | 28.838 | [Same public repository](https://github.com/SpectrAI-Initiative/InnoClaw) | Workspace context and evidence-oriented paper study |

InnoClaw occupies two versioned entries. The public checkout inspected was `67231f373deac3db29f685985a5632b6c0eb0f08`; this does not establish that its HEAD is the exact submission used for either benchmark result. Closed or unavailable engine implementations have not been imported or represented as reproduced.

The local orchestration modules are original implementations inspired by these public mechanisms. A merged feature set has not yet been shown to improve external benchmark scores.

## Execution

1. Inventory configured datasets, codebases and checkpoints before decomposing the task. Record file availability and table headers, including unavailable inputs.
2. Derive task-specific acceptance criteria. Each criterion retains its requirement, owning stage, evidence paths and satisfaction condition. A missing model or malformed rubric is an explicit failure to establish task coverage.
3. Ground those criteria in the literature synthesis without removing instruction requirements.
4. Select scientific skills for the current stage and topic. The ordinary LLM client, ACP client and native experiment tool loop receive the selected guidance.
5. Independently review the design and draft using three specialist perspectives. Reviews do not inherit the generation context and do not count as experimental evidence.
6. Verify real execution, finite measurements, every planned condition, declared statistical protocol, manuscript quality, claims and references. Task-specific criteria additionally require independent model verification and existing evidence references.
7. Return to the stage that owns an unmet criterion. Missing experiments return to experiment implementation; writing problems return to revision. Re-run downstream stages and verify again.
8. Archive invalidated stages outside `stage-*` and preserve every attempt. With the same acceptance contract, retain the prior evidence if coverage declines or equal-coverage manuscript quality regresses. Improving evidence can receive another bounded iteration; unchanged evidence and gaps stop further retries.

The engine continues to respect the project's approval gates. A failed stage remains failed when an approval gate is present. Scientific claims remain restricted when evidence is limited or the repair budget is exhausted.

## Skills

The project contains **210 pinned research and engineering skills**:

- 177 from [Scientific Agent Skills](https://github.com/K-Dense-AI/scientific-agent-skills), revision `5f2bc0341354fd5590aef76a533ada1e1e5b4e62`.
- 8 from [AutoSciRub](https://github.com/zjunlp/AutoSciRub), revision `2c6c58392ee6399e08f2526e35176f0e2c716e16`.
- 8 Apache-2.0 skills from [Anthropic](https://github.com/anthropics/skills), revision `dbd4588f9e1033efb41dad4bef2f7947c8993d44`.
- 7 MIT engineering skills from [Superpowers](https://github.com/obra/superpowers), revision `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`.
- 10 CC-BY-SA-4.0 guidance skills from [Trail of Bits](https://github.com/trailofbits/skills), revision `bf23bfb21f10a2a4b0406086d690dc8cb1b3ccf3`.

Original skill files, references and scripts are retained under `backend/agent/researchclaw/skills/library/`. Source licenses and per-file SHA-256 hashes are recorded in `sources.lock.json`. Installing this library does not install all scientific packages or grant credentials to external services. A skill requiring an unavailable tool remains procedural guidance until that tool is configured.

Selection loads at most three skills per stage with a configured 24,000-character ceiling; the compact workflow caps skill context at 7,000 characters. Phase and problem routing select the core guide plus a bounded, hash-verified relevant supporting document within the same budget. Excerpts point to the complete local procedure; the experiment agent can read its supporting resources. Additional trusted skill directories can be configured through `research_engine.skills_dirs`. Scripts are not automatically executed simply because a skill mentions them.

```bash
PYTHONPATH=backend/agent python -m researchclaw skills
PYTHONPATH=backend/agent python -m researchclaw skills --verify
PYTHONPATH=backend/agent python -m researchclaw skills --stage 9 --topic 'controlled experiment with repeated seeds'
```

These skills are installed in the **project runtime** and are available to subsequent research tasks. Older configurations with no `research_engine` section retain their prior execution policy; add the section from `examples/config_template.yaml` when resuming a project with the new engine.

## Configuration

```yaml
research_engine:
  enabled: true
  profile: quality_first
  study_type: auto
  target_quality_score: 7.0
  max_repair_rounds: 3
  council_enabled: true
  rubric_enabled: true
  skills_enabled: true
  max_skills_per_stage: 3
  skill_context_chars: 24000
  memory_context_chars: 8000
  evidence_context_chars: 48000
  shared_memory: true
  skills_dirs: []
  efficiency_approval: ""
```

The paper score is an internal review threshold, not a benchmark score. Task verification cannot declare external quality improvement. Shared experience is scoped by project name and research topic, and is presented as advice rather than current measurements.

## Artifacts and interface

`research_rubric.json` defines the contract. `research_verification.json` records each satisfied, unmet or pending criterion. `research_council.json` contains independent reviews. `research_state.json` and `research_trace.jsonl` preserve structured handoffs. `research_repair_history.json` links each repair to its archived attempt and coverage change. `research_engine_summary.json` records skills and internal verification status. `research_method_plan.json` records source-attributed method composition and task-specific obligations. `research_execution_plan.json` records artifact and control dependencies. Tool loops retain structured events under each stage's `harness/` directory.

The research interface shows satisfied and unmet requirements, evidence gaps and skills used. These files are included in deliverables. Native tool-loop requests now contribute to `traces/llm_calls.jsonl`, alongside ordinary model requests. Stage tracing uses invocation-local context so concurrent projects do not exchange attribution.

## Quality before efficiency

Use matched completed runs of the same research task and criterion contract. Preserve independent external evaluations in each run's `external_evaluation.json`:

```json
{
  "benchmark": "your-independent-evaluation",
  "task_id": "task-identifier",
  "evaluator_version": "fixed-version",
  "judge_model": "independent-judge",
  "data_revision": "fixed-input-revision",
  "score": 0.0
}
```

The score above is a schema example, not an experimental result. Populate the file from actual evaluator output. Evaluation answers and hidden grading files must not be supplied to the research agent.

```bash
PYTHONPATH=backend/agent python -m researchclaw compare-runs \
  --baseline /path/to/baseline-run \
  --candidate /path/to/candidate-run \
  --output /path/to/comparison.json
```

The comparison requires complete final checks, preserved satisfied criteria, matched independent evaluations with no score regression, and measured reductions in tokens and stage execution time. A faster run with missing quality evidence cannot approve the efficiency profile. Approval applies to the measured topic; representative tasks are needed before changing a global default.

Internal contract coverage is a diagnostic. It does not establish that a new scientific method works, that skills improve research quality, or that this project matches any leaderboard entry.

## Verification and current limits

The new integration suite runs a real seeded numerical comparison in subprocesses, detects a deliberately missing experimental condition, returns to code generation, re-runs the comparison and verifies the repaired artifacts. Generation and semantic reviews are stubbed in this offline test. It checks the engineering loop, not scientific reasoning quality.

At the initial integration, the configured model service returned HTTP 502 and delayed live comparisons. Subsequent frozen-run ResearchClawBench results are recorded below and in [the evaluation record](researchclawbench.md). The quality-first defaults remain enabled; no validated token or time improvement is claimed.


## Architecture inspection

The [architecture design](architecture-upgrade.md) maps each public source to an implemented component and its limits. `pipeline.runtime` owns stage lifecycle; `StageRegistry` provides implementations; `StageGraph` determines the compatible sequential order. A per-run lease prevents competing controllers while allowing nested bounded repairs. Stage health and handoffs record the final gate status, including missing-input failures and blocked approvals.

```bash
PYTHONPATH=backend/agent python -m researchclaw plan --config config.arc.yaml \
  --from-stage 9 --to-stage 17 --output /tmp/research-plan.json
```

This command makes no model requests and emits no API credentials. `research_engine.study_type` accepts `auto`, `comparison`, `reproduction`, `observational`, `numerical`, or `formal`. Explicit intent selects specialized review lenses and additional acceptance obligations. Ambiguous topics keep the empirical comparison default. Routing cannot remove user requirements or waive core evidence checks. Formal proof obligations remain unmet until a dedicated validator exists.

A completed review with a supported critical issue does not satisfy its review criterion. Repair retention compares the same acceptance contract and rejects loss of previously satisfied requirements even when aggregate coverage rises. Reusable advice includes the compared attempt and source hash; it is scoped experience, not evidence of scientific improvement. Skills are never rewritten automatically from one successful attempt.

All 150 former executor functions have moved into 38 modules under `steps`, `ideation`, `writing`, and `support`. The 248-line executor is a compatibility facade; production scheduling uses the common runtime and explicit built-in registry directly. Tests enforce an acyclic implementation dependency graph, no business imports of the facade, and no shared-support imports of business modules.

Dependency-driven execution remains sequential; undeclared data dependencies still require conservative suffix invalidation during repair. Stage wildcard reads exclude historical versions, future stages and outside-stage links. Writing uses current analyzed evidence. Existing threaded idea branches preserve stage guidance and skill directories. Both legacy Markdown experience and structured handoffs use project/topic isolation when the engine is enabled.

Web-created projects inherit root research-engine settings through structured YAML serialization, preserve multiline user requirements and quoted file paths, and infer the task's domain instead of inheriting the example ML domain. Independent manuscript reviews require a finite score and a valid verdict; revision or rejection cannot be overridden by a high numeric score. Unavailable reviews remain explicit and do not fabricate scores or strengths.

Paper-linked public GitHub code can supply pinned, line-addressed source evidence without another model conversation. Acquisition is bounded and credential-free; downloaded text is not installed or executed. The same scientific source-context limit prioritizes focal paper/code over background excerpts. Complete repository/configuration coverage and scientific equivalence to the published version are not presumed. V21 passed 633 relevant checks and a zero-model replay restoring actual prior scientific artifacts; only an independent benchmark can establish score gains.

V22 prioritizes complete executable statements and nearby dependencies over long Python docstrings under the same code-context budget. The existing protocol and design review record and compare source equations, weights, normalization scope and uncertainty definitions. 130 relevant checks passed. Its frozen source `59aa9a890ed3ec4a735dd31e4f9a8eae9081ef65bf0b8f9f6dcb758b8f9495fb` completed the independent Earth_000 run at **13.5/100**, with 18 research requests, 1,643.56 seconds and 1,136,995 recorded research tokens; the run's own status remains `completed_with_gaps`. Recorded tokens are not a complete provider bill. This is below v21's same-task 15.8/100 and v11's 21/100. The source coverage gains did not produce a score improvement in this run, and the goal remains unmet.
