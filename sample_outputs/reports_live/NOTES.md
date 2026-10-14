# Live DeepSeek evals: notes

Two runs of the same eval on real models, before and after the fixes the first one led to.

- **Command (both rounds):** `python -m creaition eval --live --variants 2` (baseline vs `candidate_prompt_v2`)
- **Size:** 6 briefs × 2 variants × 2 configs, plus judge calibration
- **Models:** DeepSeek official API (`deepseek-v4-pro`, `deepseek-flash`)
- **Contents of each round:** `eval_report.md` / `eval_report.json` (metrics, gate, calibration), and `runs/eval-*/` with `summary.json`, the full `trace.jsonl` and every variant's JSON. To read a variant: `python -m creaition show <path to variant JSON>`.

## round1_before_fixes/

- **Cost:** about CNY 6.5 at peak list price (CNY 5.1 for the pipeline runs, the rest for judge calibration). It ran off-peak, so the actual bill was about half.
- It led to four fixes: a larger per-variant budget (80k / CNY 0.60), schema checks on tool arguments, the list of valid feature IDs in the intent-trace hint, and over-long text cut by code. Its 33% and 25% fallback rates come from the budget being set from mock numbers.
- The only change made to this report since: its latency rows were wrongly labelled "modelled" and now say **wall-clock**, which is what they are (measured model-call time per variant).

## round2_after_fixes/

- **Cost:** about CNY 7 at peak list price (CNY 5.6 for the pipeline runs, the rest for judge calibration).
- Run on the code after the round-1 fixes and a code review. The fallback rate fell to 8%.
- It found two new problems, fixed offline afterwards (ARCHITECTURE §3.5):
  1. Baseline B2-v001 was auto-approved although the reviser had dropped the TPE overmould that made its bumper; the judge believed the change log. Two more auto-approved variants had dropped a concept feature. A new code check (`domain/drift.py`) now catches this and sends such variants to a person.
  2. The report's gate verdict is **BLOCK** with "missed a conflicting brief". That was wrong: the model had called the feasible brief B1 a hard conflict in both configs, and the old check counted any hard label, described it as a miss, and ignored that the baseline did the same.
- **The report is kept exactly as it was generated.** After fixing the gate, the saved round-2 results were checked again offline, with no model calls: code-proven conflict detection is 100% in both configs, no conflicting brief was missed, and the model made one false alarm (B1) in each config. With the fixed rules the verdict is **REVIEW** (concept variety −25%), not BLOCK.
