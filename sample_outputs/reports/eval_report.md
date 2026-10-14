# Evaluation & regression report

Generated 2026-10-14 02:33 | mode: **mock** | 32 variants per config (6 briefs)

> Offline mock models. These numbers exercise the harness end to end and show how a change is gated; they are not claims about DeepSeek/OpenAI model quality. Re-run with `--live` for real numbers.

| | config | description |
|---|---|---|
| A (baseline) | `baseline` | v1 prompts, default DeepSeek routing (flash concepts, v4-pro structure + judge, flash reviser) |
| B (candidate) | `candidate_prompt_v2` | v2 prompts: numeric DFM checklist in the structural prompt + a DFM-safe reference concept as a few-shot example |

## Gate verdict: **REVIEW**

Silent-regression signals (pass rate looks fine, outputs got worse; needs human sign-off):
- concept diversity -51% (mode collapse) while pass rate held

Bootstrap 95% CI of B - A: pass rate [+3.3, +36.7] pp; judge mean [-0.07, +0.32]; judge-score KS = 0.188.

## Metrics summary

| metric | A baseline | B candidate | delta | |
|---|---:|---:|---:|---|
| Pass rate, feasible briefs (all automated gates) | 76.7% | 96.7% | +20.0 pp | better |
| Production-ready (clean auto-approval) | 66.7% | 90.0% | +23.3 pp | better |
| Avg model cost / variant (CNY) | 0.1036 | 0.1011 | -0.002 | better |
| Avg modelled latency / variant (s) | 40.3 | 37.6 | -2.7 | better |
| Judge score (mean overall, 1-5) | 4.03 | 4.14 | +0.115 | better |
| First-draft DFM pass rate | 25.0% | 59.4% | +34.4 pp | better |
| Avg LLM revisions / variant | 0.56 | 0.44 | -0.125 | better |
| p95 modelled latency (s) | 62.7 | 60.9 | -1.9 | better |
| Avg tokens / variant | 14,247 | 13,048 | -1,198 | better |
| Concept diversity (mean pairwise distance) | 0.454 | 0.221 | -0.233 | worse |
| Judge intent-preservation (mean) | 4.38 | 4.50 | +0.125 | better |
| BOM headroom vs ceiling (passing) | 20.0% | 21.0% | +1.0 pp | better |
| DFM rule headroom (passing) | 0.459 | 0.470 | +0.011 | better |
| Deterministic-fallback rate | 0.0% | 0.0% | 0 | = |
| Schema-repair rate (invalid replies / calls) | 0.0% | 0.0% | 0 | = |
| Calls served by a fallback model route | 0.0% | 0.0% | 0 | = |
| Schema validity | 100.0% | 100.0% | 0 | = |
| Passing variants: schema-valid, all DFM rules pass, under cost ceiling | 100.0% | 100.0% | 0 | = |
| Judge vs deterministic GT agreement | 93.3% | 100.0% | +6.7 pp | better |
| Conflict detection accuracy (code) | 100.0% | 100.0% | 0 | = |
| Feasible briefs the model alone called a hard conflict | 0 | 0 | 0 | = |

## Deterministic checks per brief

| brief | A statuses | B statuses |
|---|---|---|
| B1 | {'rejected': 3, 'production_ready': 3} | {'production_ready': 6} |
| B2 | {'needs_human': 2, 'production_ready': 2, 'rejected': 2} | {'needs_human': 2, 'production_ready': 4} |
| B3 | {'production_ready': 5, 'needs_human': 1} | {'production_ready': 5, 'rejected': 1} |
| B4 | {'production_ready': 6} | {'production_ready': 6} |
| B5 | {'rejected': 2} | {'rejected': 2} |
| B6 | {'production_ready': 4, 'rejected': 2} | {'production_ready': 6} |

Conflict detection by code: A 100%, B 100%. Conflicting briefs with no hard conflict at all: A 0, B 0 (must be 0). Feasible briefs the model alone called a hard conflict: A 0, B 0 (their variants all go to a human). Conflicting-brief variants auto-approved: A 0, B 0 (must be 0); held for a human or rejected: A 100%, B 100%. Pass rates above exclude the conflicting brief (feasible briefs: B1, B2, B3, B4, B6).

## LLM-as-judge calibration

> The mock judge and the ground truth are built from similar signals, so high agreement here only shows that the calibration machinery works. It says nothing about a real judge: see `sample_outputs/reports_live/` for the live numbers.

**A (baseline)** - agreement with deterministic ground truth: 93.3% over 30 variants, Cohen's kappa = 0.793; confusion {'judge=not|gt=not': 5, 'judge=adherent|gt=adherent': 23, 'judge=not|gt=adherent': 2}. Planted-defect sensitivity: 100.0% of 8 mutated specs scored brief_adherence <= 3. Self-consistency (re-judge at T=0.7): mean |delta overall| = 0.208, verdict flip rate = 0.0%.

**B (candidate_prompt_v2)** - agreement with deterministic ground truth: 100.0% over 30 variants, Cohen's kappa = 1.0; confusion {'judge=adherent|gt=adherent': 29, 'judge=not|gt=not': 1}. Planted-defect sensitivity: 100.0% of 8 mutated specs scored brief_adherence <= 3. Self-consistency (re-judge at T=0.7): mean |delta overall| = 0.167, verdict flip rate = 0.0%.

| variant | archetype | GT after mutation | adherence before -> after | detected |
|---|---|---|---|---|
| B1-v002 | cost_sensitive | False | 4 -> 1 | yes |
| B1-v003 | cost_sensitive | False | 5 -> 1 | yes |
| B1-v004 | cost_sensitive | False | 4 -> 1 | yes |
| B2-v001 | rugged | False | 5 -> 1 | yes |
| B2-v002 | rugged | False | 5 -> 2 | yes |
| B2-v005 | rugged | False | 5 -> 1 | yes |
| B2-v006 | rugged | False | 5 -> 1 | yes |
| B3-v001 | premium_home | False | 5 -> 1 | yes |

## Benchmark: robustness under chaos (baseline config + rate-based faults)

Faults injected: {'timeout': 39, 'malformed_prose': 4, 'malformed_types': 3, 'rate_limit': 6, 'tool_error': 40, 'server_error': 4, 'malformed_missing': 5, 'malformed_truncated': 4}

| | clean | chaos |
|---|---:|---:|
| completion rate (not FAILED) | 100.0% | 100.0% |
| pass rate | 76.7% | 76.7% |
| avg model cost / variant (CNY) | 0.1036 | 0.1147 |
| degraded-evidence rate | - | 3.1% |
| fallback rate | - | 0.0% |
| schema repair rate | - | 8.5% |
| calls served by fallback model | - | 0.0% |

Status mix under chaos: {'rejected': 9, 'production_ready': 19, 'needs_human': 4}. Breakers at end: {'llm:deepseek:deepseek-v4-pro': 'closed', 'llm:deepseek:deepseek-flash': 'closed', 'tool:materials_lookup': 'closed', 'tool:dfm_validate': 'closed', 'tool:tooling_quote': 'closed'}

## Benchmark: memory / teach effect

Run 1 learns lessons from its own first-draft DFM failures (plus one designer-taught lesson); run 2 reads them.

| | before lessons | after lessons |
|---|---:|---:|
| first_pass_dfm_rate | 0.25 | 0.625 |
| avg_llm_revisions | 0.5625 | 0.5625 |
| avg_autofixes | 0.6562 | 0.2812 |
| avg_model_cost_cny | 0.10356 | 0.10472 |
| gate_pass_rate | 0.7667 | 0.7667 |

Lessons in memory:
- `L-91a32d3325` (human, rule RIB_RATIO): Size ribs at 50% of the nominal wall (40-60% allowed); designers saw sink marks.
- `L-auto-inner_radius` (auto, rule INNER_RADIUS): Inner radius must be at least 0.5 x the wall thickness. (seen in 7/32 first drafts)
- `L-auto-packaging_fit` (auto, rule PACKAGING_FIT): Envelope height must be >= 22 mm + 2 x wall to fit the stacked PCB and battery. (seen in 7/32 first drafts)
- `L-auto-vent_open_area` (auto, rule VENT_OPEN_AREA): Intake vents need >= 15% open area; 7 slots of 1.2 x 16 mm in a 20 x 20 mm zone gives ~34%. (seen in 6/32 first drafts)
