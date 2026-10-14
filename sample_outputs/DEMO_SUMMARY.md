# Sample outputs

Everything in this folder was produced by `python -m creaition demo`: offline mock models, no API key, and the same designs on every run with the same Python version (the exact number of injected retries depends on timing). The one exception is `reports_live/`, which comes from real DeepSeek models and is kept when the demo is run again. Mock numbers show that the machinery works; they are not claims about real model quality.

## 1. Start here: the final output

Each `report.md` is a run's final output: per brief, the outcome, then a design card for each shortlisted variant with a to-scale sketch, the 2D concept, the 3D structure, DFM, cost, judge scores and what changed from the concept.

| report | what it covers |
|---|---|
| [`runs/demo-designs/report.md`](runs/demo-designs/report.md) | **all 6 evaluation briefs**, 3 variants each (the impossible B5 gets 2 examples after relaxing): 17 variants, 10 pending_approval, 5 rejected, 2 needs_human. Console output: `00_final_designs_run.log`. |
| [`runs/demo-faults/report.md`](runs/demo-faults/report.md) | the rugged brief B2, 5 variants made while faults were injected: 2 pending_approval, 2 rejected, 1 needs_human |
| [`runs/demo-ask-B5/report.md`](runs/demo-ask-B5/report.md) and [`infeasible_B5.md`](runs/demo-ask-B5/infeasible_B5.md) | the deliberately impossible brief B5: the designer refused one relaxation, so there are no variants, only an explanation of why it cannot be made and what would fix it |
| [`reports_live/round2_after_fixes/runs/eval-baseline/report.md`](reports_live/round2_after_fixes/runs/eval-baseline/report.md) | the same kind of report from **real DeepSeek models** (6 briefs, 2 variants each) |

Status words: `pending_approval` passed every automated check and waits for a person to sign off; `needs_human` passed with caveats a person must read; `rejected` failed a check (the reason is listed); `production_ready` was approved.

## 2. Reliability: one file per scenario

| file | scenario | what to look for |
|---|---|---|
| `01_fault_injection_run.log` | brief B2 with scripted faults: 30 injected (18 server_error, 4 rate_limit, 3 timeout, 2 malformed_types, 1 malformed_prose, 1 malformed_truncated, 1 tool_error) | lines tagged `[FAULT]` followed by `[RETRY]`, `[REPAIR]`, `[FALLBACK]` and `[RECOVERED]`; `[CIRCUIT]` when the breaker for llm:deepseek:deepseek-v4-pro opens and later variants skip it; `[THROTTLE]` when a 429 halves the calls in flight. No variant failed. |
| `01_trace_tree_B2-v002.txt`, `01_trace_tree_B2-v003.txt` | the trace of one variant from that run, as a tree | each step, each model or tool call under it, and the fault and recovery events on each call |
| `02a_run_killed_after_30_steps.log` | run `demo-resume` (3 briefs × 3 variants, saved in `runs/demo-resume/`); the process is killed (exit 137) after 30 steps | the `[CRASH]` line |
| `02b_todos_after_crash.txt` | the saved plan right after the crash | unfinished items and variants still marked `running` |
| `02c_resume.log` | `resume` on the same run | `replayed from checkpoint: 30` (no model calls for those) |
| `02d_resume_equivalence.txt` | resumed run vs. a run with no crash | RESULT: 9/9 variants identical to the uninterrupted run; 30 + 52 = 82 executed steps vs 82 without a crash (no committed step was re-run or re-billed) |
| `03_hitl_ask_todos.log` | approval and questions | a person approves a variant; a bot is refused on a variant with caveats; `ask` answers from the run's own records |
| `04_interactive_ask_conflicting_brief.log` | the Ask gate on the impossible brief B5 | three proposed relaxations; the designer refuses the mass one, so no variants are made |
| `05_teach_memory.log` | teaching | a designer adds a lesson; `lessons` lists memory |

## 3. Evaluation

| file | what it is |
|---|---|
| [`reports/eval_report.md`](reports/eval_report.md) (+ `.json`) | baseline vs. candidate (prompt v2) on the 6 evaluation briefs: gate verdict, metrics, per-brief results, judge calibration, chaos and memory benchmarks |
| `06_eval_harness.log` | the command that made that report |
| [`reports_live/`](reports_live/NOTES.md) | the same eval on real DeepSeek models, before (`round1_before_fixes/`) and after (`round2_after_fixes/`) the fixes it led to; start with `NOTES.md` |

Mock eval in one line: gate **REVIEW** (concept diversity -51% (mode collapse) while pass rate held). Pass rate 77% → 97%, judge average 4.03 → 4.14, model cost per variant CNY 0.104 → 0.101, concept variety 0.45 → 0.22. The judge agrees with the code-based ground truth on 93% of baseline variants (κ 0.793); the live runs show a real judge does much worse, see `reports_live/`.

## 4. Inside each run folder

| file | what it is |
|---|---|
| `report.md`, `sketches/` | the readable final output and its sketches |
| `variants/<id>.json` | everything about one variant: concept, structure, DFM report, cost, judge review, step history, token use |
| `summary.json` | run totals, per-brief outcome and shortlist, each brief's constraint contract |
| `trace.jsonl` | every span and event of the run, one JSON object per line |

There is no database in this folder, so pass file paths to the tools. The commands below show one variant in readable form, print the trace of one variant as a tree, and rebuild a run's `report.md`:

```bash
python -m creaition show sample_outputs/runs/demo-designs/variants/B3-v001.json
python -m creaition --workspace sample_outputs trace demo-faults --variant B2-v002
python -m creaition report sample_outputs/runs/demo-designs
```
