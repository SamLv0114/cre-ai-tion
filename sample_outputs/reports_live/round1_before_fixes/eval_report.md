# Evaluation & regression report

Generated 2026-10-13 14:32 | mode: **live** | 12 variants per config (6 briefs)

| | config | description |
|---|---|---|
| A (baseline) | `baseline` | v1 prompts, default DeepSeek routing (flash concepts, v4-pro structure + judge, flash reviser) |
| B (candidate) | `candidate_prompt_v2` | v2 prompts: numeric DFM checklist in the structural prompt + a DFM-safe reference concept as a few-shot example |

## Gate verdict: **REVIEW**

Silent-regression signals (pass rate looks fine, outputs got worse; needs human sign-off):
- DFM headroom -23% (specs hugging the rule limits)

Bootstrap 95% CI of B - A: pass rate [+0.0, +0.0] pp; judge mean [-0.60, +0.51]; judge-score KS = 0.25.

## Metrics summary

| metric | A baseline | B candidate | delta | |
|---|---:|---:|---:|---|
| Pass rate, feasible briefs (all automated gates) | 100.0% | 100.0% | 0 | = |
| Production-ready (clean auto-approval) | 70.0% | 70.0% | 0 | = |
| Avg model cost / variant (CNY) | 0.2211 | 0.2044 | -0.017 | better |
| Avg wall-clock latency / variant (s) | 28.3 | 24.8 | -3.6 | better |
| Judge score (mean overall, 1-5) | 4.56 | 4.51 | -0.042 | worse |
| First-draft DFM pass rate | 0.0% | 0.0% | 0 | = |
| Avg LLM revisions / variant | 0.83 | 0.58 | -0.250 | better |
| p95 wall-clock latency (s) | 39.1 | 31.8 | -7.3 | better |
| Avg tokens / variant | 27,159 | 22,669 | -4,489 | better |
| Concept diversity (mean pairwise distance) | 0.465 | 0.420 | -0.046 | worse |
| Judge intent-preservation (mean) | 4.50 | 4.42 | -0.083 | worse |
| BOM headroom vs ceiling (passing) | 20.0% | 22.6% | +2.7 pp | better |
| DFM rule headroom (passing) | 0.396 | 0.306 | -0.090 | worse |
| Deterministic-fallback rate | 33.3% | 25.0% | -8.3 pp | better |
| Schema-repair rate (invalid replies / calls) | 10.0% | 2.0% | -8.0 pp | better |
| Calls served by a fallback model route | 1.8% | 0.0% | -1.8 pp | better |
| Schema validity | 100.0% | 100.0% | 0 | = |
| Judge vs deterministic GT agreement | 60.0% | 60.0% | 0 | = |
| Conflict detection accuracy | 100.0% | 100.0% | 0 | = |

## Deterministic checks per brief

| brief | A statuses | B statuses |
|---|---|---|
| B1 | {'production_ready': 2} | {'production_ready': 2} |
| B2 | {'needs_human': 2} | {'needs_human': 2} |
| B3 | {'production_ready': 1, 'needs_human': 1} | {'production_ready': 2} |
| B4 | {'production_ready': 2} | {'needs_human': 1, 'production_ready': 1} |
| B5 | {'rejected': 2} | {'rejected': 2} |
| B6 | {'production_ready': 2} | {'production_ready': 2} |

Conflict detection accuracy: A 100%, B 100%. Conflicting-brief variants auto-approved: A 0, B 0 (must be 0); held for a human or rejected: A 100%, B 100%. Pass rates above exclude the conflicting brief (feasible briefs: B1, B2, B3, B4, B6).

## LLM-as-judge calibration

**A (baseline)** - agreement with deterministic ground truth: 60.0% over 10 variants, Cohen's kappa = 0.0; confusion {'judge=adherent|gt=adherent': 6, 'judge=adherent|gt=not': 4}. Planted-defect sensitivity: 83.3% of 6 mutated specs scored brief_adherence <= 3. Self-consistency (re-judge at T=0.7): mean |delta overall| = 0.146, verdict flip rate = 0.0%.

**B (candidate_prompt_v2)** - agreement with deterministic ground truth: 60.0% over 10 variants, Cohen's kappa = 0.0; confusion {'judge=adherent|gt=adherent': 6, 'judge=adherent|gt=not': 4}. Planted-defect sensitivity: 83.3% of 6 mutated specs scored brief_adherence <= 3. Self-consistency (re-judge at T=0.7): mean |delta overall| = 0.208, verdict flip rate = 0.0%.

| variant | archetype | GT after mutation | adherence before -> after | detected |
|---|---|---|---|---|
| B1-v001 | cost_sensitive | False | 5 -> 2 | yes |
| B1-v002 | cost_sensitive | False | 5 -> 2 | yes |
| B4-v001 | sustainable | False | 5 -> 2 | yes |
| B4-v002 | sustainable | False | 5 -> 1 | yes |
| B6-v001 | balanced | False | 5 -> 2 | yes |
| B6-v002 | balanced | False | 5 -> 4 | NO |
