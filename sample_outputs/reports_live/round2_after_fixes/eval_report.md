# Evaluation & regression report

Generated 2026-10-14 00:43 | mode: **live** | 12 variants per config (6 briefs)

| | config | description |
|---|---|---|
| A (baseline) | `baseline` | v1 prompts, default DeepSeek routing (flash concepts, v4-pro structure + judge, flash reviser) |
| B (candidate) | `candidate_prompt_v2` | v2 prompts: numeric DFM checklist in the structural prompt + a DFM-safe reference concept as a few-shot example |

## Gate verdict: **BLOCK**

Hard regressions (block the release):
- missed a conflicting brief
Silent-regression signals (pass rate looks fine, outputs got worse; needs human sign-off):
- concept diversity -25% (mode collapse) while pass rate held

Bootstrap 95% CI of B - A: pass rate [+0.0, +30.0] pp; judge mean [-0.58, +0.58]; judge-score KS = 0.136.

## Metrics summary

| metric | A baseline | B candidate | delta | |
|---|---:|---:|---:|---|
| Pass rate, feasible briefs (all automated gates) | 90.0% | 100.0% | +10.0 pp | better |
| Production-ready (clean auto-approval) | 80.0% | 70.0% | -10.0 pp | worse |
| Avg model cost / variant (CNY) | 0.2506 | 0.2132 | -0.037 | better |
| Avg wall-clock latency / variant (s) | 36.6 | 30.3 | -6.3 | better |
| Judge score (mean overall, 1-5) | 4.39 | 4.39 | -0.005 | worse |
| First-draft DFM pass rate | 0.0% | 8.3% | +8.3 pp | better |
| Avg LLM revisions / variant | 1.25 | 1.00 | -0.250 | better |
| p95 wall-clock latency (s) | 51.0 | 44.5 | -6.5 | better |
| Avg tokens / variant | 36,156 | 31,010 | -5,146 | better |
| Concept diversity (mean pairwise distance) | 0.522 | 0.391 | -0.131 | worse |
| Judge intent-preservation (mean) | 4.45 | 4.50 | +0.045 | better |
| BOM headroom vs ceiling (passing) | 22.7% | 18.4% | -4.3 pp | worse |
| DFM rule headroom (passing) | 0.438 | 0.393 | -0.045 | worse |
| Deterministic-fallback rate | 8.3% | 8.3% | 0 | = |
| Schema-repair rate (invalid replies / calls) | 4.5% | 1.6% | -2.9 pp | better |
| Calls served by a fallback model route | 0.0% | 0.0% | 0 | = |
| Schema validity | 100.0% | 100.0% | 0 | = |
| Judge vs deterministic GT agreement | 55.6% | 60.0% | +4.4 pp | better |
| Conflict detection accuracy | 83.3% | 83.3% | 0 | = |

## Deterministic checks per brief

| brief | A statuses | B statuses |
|---|---|---|
| B1 | {'needs_human': 1, 'rejected': 1} | {'needs_human': 2} |
| B2 | {'production_ready': 2} | {'needs_human': 1, 'production_ready': 1} |
| B3 | {'production_ready': 2} | {'production_ready': 2} |
| B4 | {'production_ready': 2} | {'production_ready': 2} |
| B5 | {'rejected': 2} | {'rejected': 2} |
| B6 | {'production_ready': 2} | {'production_ready': 2} |

Conflict detection accuracy: A 83%, B 83%. Conflicting-brief variants auto-approved: A 0, B 0 (must be 0); held for a human or rejected: A 100%, B 100%. Pass rates above exclude the conflicting brief (feasible briefs: B1, B2, B3, B4, B6).

## LLM-as-judge calibration

**A (baseline)** - agreement with deterministic ground truth: 55.6% over 9 variants, Cohen's kappa = 0.0; confusion {'judge=adherent|gt=not': 4, 'judge=adherent|gt=adherent': 5}. Planted-defect sensitivity: 80.0% of 5 mutated specs scored brief_adherence <= 3. Self-consistency (re-judge at T=0.7): mean |delta overall| = 0.292, verdict flip rate = 0.0%.

**B (candidate_prompt_v2)** - agreement with deterministic ground truth: 60.0% over 10 variants, Cohen's kappa = 0.0; confusion {'judge=adherent|gt=adherent': 6, 'judge=adherent|gt=not': 4}. Planted-defect sensitivity: 100.0% of 6 mutated specs scored brief_adherence <= 3. Self-consistency (re-judge at T=0.7): mean |delta overall| = 0.146, verdict flip rate = 0.0%.

| variant | archetype | GT after mutation | adherence before -> after | detected |
|---|---|---|---|---|
| B2-v001 | rugged | False | 5 -> 4 | NO |
| B4-v001 | sustainable | False | 5 -> 1 | yes |
| B4-v002 | sustainable | False | 5 -> 1 | yes |
| B6-v001 | balanced | False | 5 -> 2 | yes |
| B6-v002 | balanced | False | 5 -> 2 | yes |
