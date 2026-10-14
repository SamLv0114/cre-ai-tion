# cre[ai]tion: multi-agent design pipeline prototype

A working multi-agent pipeline for the assessment task: designing the housing of a portable air-quality detector. A brief becomes a **2D concept spec**, then a **3D-ready structural spec**, then a **DFM, cost and quality review**. Each brief fans out into N variants. Every step is checkpointed, the pipeline recovers from injected faults, and it comes with an evaluation harness and a regression gate.

- **Part 1 (system design):** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and the diagram [docs/architecture.svg](docs/architecture.svg)
- **Trade-offs, limits and next steps:** [DECISIONS.md](DECISIONS.md)
- **The final output (design cards with sketches):** from real DeepSeek models, [round 2 baseline](sample_outputs/reports_live/round2_after_fixes/runs/eval-baseline/report.md); from the offline demo, [all 6 briefs](sample_outputs/runs/demo-designs/report.md)
- **Evidence from a sample run:** [sample_outputs/DEMO_SUMMARY.md](sample_outputs/DEMO_SUMMARY.md)
- **Evaluation report:** [sample_outputs/reports/eval_report.md](sample_outputs/reports/eval_report.md)

## Run it (offline, one command)

```bash
./run.sh
```

`run.sh` picks the newest Python 3.9+ on the machine (set `PYTHON=...` to choose one), creates `.venv` the first time if `pydantic` and `pytest` are missing (the only two dependencies; Python 3.9 also gets a small typing backport), then runs the full offline demo (about 20 s) and the 90 tests (about 20 s; they run the whole eval harness again). No API key is needed.

The demo runs every scenario in turn and rewrites `sample_outputs/` (details in [`sample_outputs/DEMO_SUMMARY.md`](sample_outputs/DEMO_SUMMARY.md)):

| # | Scenario | Run folder | Output files |
|---|---|---|---|
| 00 | **The final output**: all 6 evaluation briefs, 3 variants each | `runs/demo-designs/` | `00_final_designs_run.log`, `runs/demo-designs/report.md` |
| 01 | Scripted faults (server errors, 429s, timeouts, broken JSON, tool errors) and their recovery | `runs/demo-faults/` | `01_fault_injection_run.log`, `01_trace_tree_*.txt` |
| 02 | Process killed after 30 steps, then resumed and compared with a run that never crashed | `runs/demo-resume/` | `02a`–`02d` |
| 03 | Human approval, a bot refused on a variant with caveats, questions about a run | (uses `demo-faults`) | `03_hitl_ask_todos.log` |
| 04 | The Ask gate on the impossible brief B5; the designer refuses one relaxation | `runs/demo-ask-B5/` | `04_interactive_ask_conflicting_brief.log` |
| 05 | Teaching an agent a lesson, then listing memory | | `05_teach_memory.log` |
| 06 | The evaluation: baseline vs candidate on 6 briefs × 6 variants, plus chaos and memory benchmarks | | `06_eval_harness.log`, `reports/eval_report.md` |

`reports_live/` (the real DeepSeek results) is never touched by the demo: rebuilding it needs an API key and costs money.

Or step by step (Python ≥ 3.9): install the dependencies, run the demo, run the tests.

```bash
pip install -r requirements.txt
python -m creaition demo
python -m pytest
```

By default everything runs **offline** against mock models. No API key is needed, and on a given Python version the results are the same every run (the `random` module differs slightly between Python versions, so mock numbers can shift a little; timing can change how many retries happen). The mock is a simulator: each mock model makes its own kinds of mistakes (DFM slips, design errors, imperfect revisions, a biased and noisy judge), so the review loop, the repair path and the judge checks have something real to catch. Mock numbers show that the machinery works; they say nothing about real model quality.

### Live models (DeepSeek official API, optionally an OpenAI judge)

Put the keys in a `.env` file in the project root. It is git-ignored, never zipped and never printed:

```
DEEPSEEK_API_KEY=sk-...
OPENAI_API_KEY=sk-...
```

`OPENAI_API_KEY` is optional; it is only used with `--provider mixed`. Then, in order: one brief on DeepSeek only (about CNY 0.5), the same with OpenAI `gpt-4.1-mini` as the judge, and the full live regression report (about CNY 6–8). Costs are at DeepSeek's peak list price; the off-peak window is half price.

```bash
python -m creaition run --live --brief-ids B4 --variants 2
python -m creaition run --live --provider mixed --brief-ids B4
python -m creaition eval --live --variants 2
```

| Preset | Models | Why |
|---|---|---|
| `deepseek` (default) | `deepseek-v4-pro` for reading the brief, the 3D structure and the judge; `deepseek-flash` for concepts, revisions and questions. DeepSeek's Anthropic-compatible endpoint is used as a second network path to the same models. | One key, real list prices. The judge is a different *model* from the concept generator, but from the same vendor. |
| `mixed` | The same, except the judge is OpenAI `gpt-4.1-mini`, with DeepSeek as its fallback. | A judge from another vendor is less likely to favour its own kind of output. OpenAI is used for nothing else. |

**Spending limits for live runs** (all in `creaition/config.py`):
- 80k tokens / CNY 0.60 per variant.
- CNY 10 per run: after that, no new variants start.
- CNY 3 per run for OpenAI: after that, its routes are skipped and calls go to DeepSeek.

Live results are in [sample_outputs/reports_live/](sample_outputs/reports_live/), from two runs of 6 briefs × 2 variants per config (about CNY 6.5–7 each at list price): `round1_before_fixes/` and `round2_after_fixes/`. What they found, and what was changed because of it, is in ARCHITECTURE §3.5.

## Looking at the results

**The final output of a run is its `report.md`.** The ones that ship with this submission are already in `sample_outputs/`: start with [`sample_outputs/runs/demo-designs/report.md`](sample_outputs/runs/demo-designs/report.md) (all 6 briefs), and the live DeepSeek one in [`sample_outputs/reports_live/round2_after_fixes/runs/eval-baseline/report.md`](sample_outputs/reports_live/round2_after_fixes/runs/eval-baseline/report.md). A run you start yourself with `python -m creaition run` writes its own to `workspace/runs/<run_id>/report.md` and prints that path at the end. For each brief it gives the outcome (cost ceiling, conflicts, or why it cannot be made), then a design card for each shortlisted variant: a to-scale front and side sketch drawn from the spec, the 2D concept (form, CMF, layout), the 3D structure (parts, walls, draft, closure, sealing), the DFM result, cost, judge scores, what changed from the concept, and where each piece of design intent is kept. `python -m creaition report <run folder or run id>` rebuilds it from a saved run folder; that is how the live runs got theirs.

Each variant's full result is also one JSON file. `show` prints it in readable form: the first command lists a run's variants with their status and reason, the second shows one variant from the workspace, the third shows a saved variant file.

```bash
python -m creaition show <run_id>
python -m creaition show <run_id> B3-v001
python -m creaition show sample_outputs/reports_live/round2_after_fixes/runs/eval-baseline/variants/B2-v001.json
```

The last example is the live variant that lost its bumper (ARCHITECTURE §3.5, round 2): the output ends its structure section with `changed from the concept (code check): dropped tpe_overmold`.

## How to read the code

The core pipeline is about 2,000 lines. Code paths in this README are inside `creaition/` (so `eval/harness.py` is `creaition/eval/harness.py`; the briefs are in the top-level `eval/briefs/`). Suggested order:

1. `orchestrator/planner.py` decides the next step for a variant. It holds every loop guard and the production-ready gate.
2. `orchestrator/state.py` and `orchestrator/executor.py`: the read-only variant state, how one step is run, saved and replayed, and why resume does not pay twice.
3. `llm/structured.py` and `llm/router.py`: how one model call stays reliable (parse, validate, repair, retry, fall back).
4. `agents/critic.py`, `domain/dfm_rules.py` and `agents/reviser.py`: the review-and-revise loop. Rules are code, formula fixes are code, trade-offs are made by the LLM.
5. `orchestrator/runner.py` puts it together: brief preparation, fan-out over variants, then fan-in.

The rest is supporting code you can skim:
- `llm/mock.py` is the offline model simulator.
- `orchestrator/quality.py` is the per-brief quality breaker.
- `domain/drift.py` checks what the final structure dropped from the concept.
- `show.py` prints a variant's result; `design_report.py` writes the run's `report.md` and sketches.
- `eval/` is the evaluation harness.
- `cli.py` and `demo.py` are entry points.
- `domain/schemas.py` holds the data shapes.

## What is in `sample_outputs/`

The evidence that ships with the submission, made by `python -m creaition demo` (offline, repeatable), plus the live DeepSeek results. [`sample_outputs/DEMO_SUMMARY.md`](sample_outputs/DEMO_SUMMARY.md) describes every file; in short:

| Path | What it is |
|---|---|
| `runs/demo-designs/report.md` | **The final output**: the design report for all 6 evaluation briefs, 3 variants each (17 variants; the impossible B5 gets 2 examples after relaxing) |
| `runs/demo-faults/`, `runs/demo-resume/` | The runs behind the fault-injection and kill-and-resume scenarios, each with its own `report.md` |
| `runs/demo-ask-B5/` | The deliberately impossible brief: no variants, `infeasible_B5.md` explains why and what would fix it |
| `01_*.log`, `01_trace_tree_*.txt` | Injected faults and how each one was recovered (requirement: a trace showing a fault and its recovery) |
| `02a`–`02d` | The process killed after 30 steps, then resumed; the resumed run matches a run with no crash |
| `03`–`05` | Human approval, questions about a run, the Ask gate on B5, teaching a lesson |
| `reports/eval_report.md`, `06_eval_harness.log` | The evaluation report: metrics and regression comparison, baseline vs candidate (mock) |
| `reports_live/` | The same evaluation on real DeepSeek models, before and after fixes; start with `NOTES.md` |

`sample_outputs/` has no database, so pass file paths: `python -m creaition show sample_outputs/runs/demo-designs/variants/B3-v001.json`.

**`workspace/` vs `sample_outputs/`.** `workspace/` is where your own `run` commands write (a database plus one folder per run, so `show <run_id>`, `approve` and `resume` work). It is scratch space: `make clean` and `make zip` delete it, and it is not submitted. `sample_outputs/` is the fixed evidence that is submitted; only `python -m creaition demo` rewrites it, and `reports_live/` is never overwritten.

## Commands

| Command | What it does |
|---|---|
| `python -m creaition run [--config configs/X.json] [--brief-ids B1 B5] [--variants N] [--live] [--provider deepseek\|mixed]` | Run the pipeline. Prints a summary, the todo list and where the results are. |
| `python -m creaition run --config configs/fault_demo.json --brief-ids B2 --variants 5` | Scripted faults: broken JSON, 429, slow and failing tools, a model outage (the breaker opens). |
| `python -m creaition run --crash-after-steps 30`, then `python -m creaition resume <run_id>` | Simulate `kill -9` in the middle of a run, then resume from the checkpoints. |
| `python -m creaition resume <run_id> --ignore-quality-breaker` | Finish a brief that the quality breaker paused, after a person has looked at why. |
| `python -m creaition run --interactive --brief-ids B5` | **Ask**: the system asks you about conflicts and relaxations in the terminal. |
| `python -m creaition todos <run_id>` | **Todo list**: the plan and the status of each variant (also works after a crash). |
| `python -m creaition trace <run_id> [--variant B2-v002]` | Show the trace of a run or of one variant as a tree. |
| `python -m creaition show <run_id> [variant]` or `show <variant.json>` | A variant's result in readable form, or the list of a run's variants. |
| `python -m creaition report <run folder or run_id>` | Rebuild a run's design report (`report.md` + sketches) from its saved folder. |
| `python -m creaition pending <run_id>` / `approve <run_id> <variant> [--reject]` | **Human approval** gate. |
| `python -m creaition ask <run_id> "Why was B2-v003 rejected?"` | **Ask** about a run: answers use only the run's own records. |
| `python -m creaition teach "<lesson>" --agent structural_modeler --rule RIB_RATIO` / `lessons` | **Teach** an agent; list what is in **memory**. |
| `python -m creaition eval [--baseline cfg] [--candidate cfg] [--benchmark] [--strict]` | Evaluation, regression report, chaos and memory **benchmark**. `--strict` sets the exit code for CI. |
| `python -m creaition runs` | List the runs in the workspace (`./workspace` by default; change it with `--workspace DIR`). |

Results go to `workspace/` (a SQLite database plus one folder per run, each with its `report.md`). `make clean` and `make zip` delete `workspace/` and `reports/`.

## Requirement → implementation map

Code paths are inside `creaition/`.

| Requirement | Where |
|---|---|
| Orchestrator coordinating agents | `orchestrator/runner.py` (brief preparation, bounded fan-out, fan-in), `orchestrator/executor.py` |
| Explicit typed state, no global blob | `domain/schemas.py` (frozen Pydantic artifacts), `orchestrator/state.py` (read-only `VariantState` + pure reducer) |
| Router / planner deciding the next step | `orchestrator/planner.py` (`next_step`, `gate`) |
| Critic-and-revise loop with a hard cap | DFM critic (`agents/critic.py`, code) → AutoFixer / reviser (`agents/reviser.py`, LLM). Limits: 3 revisions, 3 tool calls, 18 steps, repeated-spec detection, budget. |
| ≥ 2 tools | `tools/services.py`: `materials_lookup`, `tooling_quote`, `dfm_validate`, `cost_what_if` (the reviser itself can call `materials_lookup` and `cost_what_if`) |
| Schema-validated structured output, bounded repair, clean fallback | `llm/structured.py` (parse → Pydantic → repair ≤ 2 → next model → error); every agent has a code fallback |
| Graceful handling of tool errors and timeouts | `tools/registry.py` (retry → snapshot / rate table / local rules, marked `degraded` → `NEEDS_HUMAN`) |
| Retries with backoff, per-step timeouts | `reliability/retry.py` (timeout per attempt, full jitter), `executor.py` (step timeout) |
| Idempotency keys | `reliability/idempotency.py`, `executor.py` (`H(run, variant, step, seq, inputs)`, `INSERT OR IGNORE`) |
| Circuit breaker + loop guard | `reliability/circuit.py` (per model endpoint and tool), `planner.py` guards, `runner.py` run-level breaker, `orchestrator/quality.py` per-brief quality breaker |
| Checkpoint and resume | `state/store.py` (step log, variant status table), `resume` command; shown to match a run with no crash (`tests/test_orchestrator.py`) |
| Fault injection visible in logs | `reliability/faults.py`, `llm/faulty.py`, `configs/fault_demo.json`, `sample_outputs/01_*` |
| ≥ 5 test briefs + schema / DFM / cost checks | `eval/briefs/` (6: cost, rugged, premium, sustainable, **conflicting**, balanced), `eval/harness.py` |
| LLM-as-judge + agreement with code-based ground truth | `agents/judge.py`, `eval/ground_truth.py`: κ, planted defects, repeat scoring |
| Regression report: pass rate, cost, latency, judge score | `eval/report.py` → `sample_outputs/reports/eval_report.md` |
| Per-step tracing | `observability/tracing.py` (JSON-line spans + tagged console lines), `runs/<id>/trace.jsonl` |
| A test command that runs the eval harness | `python -m pytest` (`tests/test_eval_harness.py`) |

**Bonus items:**

- **Routing across models and vendors, with fallback:** `llm/router.py`, `config.py`. Each role has a fallback chain: model → model → second API endpoint. The `mixed` preset uses two vendors, and the router enforces a spending cap per vendor.
- **Caching across similar briefs:** a semantic cache that also compares the numbers in the brief, plus lessons (`state/memory.py`). Within a run, step results are reused on retry and resume.
- **Parallel fan-out with backpressure and a concurrency limit:** bounded queue + worker pool, token bucket, AIMD (`reliability/limiter.py`).
- **Human approval gate:** `orchestrator/hitl.py`.
- **Ask and todo list:** `agents/resolver.py` (clarify gate), `orchestrator/ask.py`, `orchestrator/todos.py`.
- **Memory and teaching:** `state/memory.py`, `teach` command.
- **Benchmark:** `eval --benchmark` (chaos robustness + memory effect).
- **Long-running work:** saved plan and todos, run budget, resume, per-brief quality breaker.

## Repository layout

```
creaition/
  domain/         constants (the one place for limits), schemas, geometry, DFM rules, cost model, baselines, concept drift check
  llm/            live clients (DeepSeek OpenAI + Anthropic endpoints, OpenAI), offline mock, fault wrapper, router, structured output
  agents/         interpreter, resolver + Ask gate, concept, structural, critic + autofix, reviser, cost, judge, explainer
  orchestrator/   state + reducer, planner + gate, step executor, runner (fan-out), quality breaker, todos, approvals, ask
  reliability/    retry, circuit breaker, limiter (token bucket + AIMD), idempotency, budget, faults
  state/          SQLite store (step log, variant status, todos, approvals, lessons, cache), memory + semantic cache
  observability/  tracer (JSON-line spans), trace tree
  eval/           harness, ground truth + planted defects, report
  prompts.py      versioned prompts (v1, v2); their hash goes into idempotency keys
  show.py, design_report.py   readable variant view; the run's report.md and sketches
  cli.py, demo.py, config.py  command line, offline demo, run settings and model presets
configs/          baseline, candidate_prompt_v2, candidate_flash_structural, fault_demo, chaos
eval/briefs/      6 evaluation briefs with expected results
tests/            90 tests (domain, reliability, orchestrator, eval harness)
docs/             ARCHITECTURE.md (Part 1), architecture.svg
sample_outputs/   logs, traces and eval report from `python -m creaition demo`; reports_live/ from two live DeepSeek evals
```
