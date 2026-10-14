# DECISIONS

The main trade-offs behind the prototype, its known limits, and what I would do with more time. The full design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 1. Assumptions

| Area | Assumption |
|---|---|
| Product | The housing of a portable, battery-powered air-quality detector: PCB, battery, display, and a sensor / fan module that pulls air through an intake. |
| BOM | "BOM target ≤ ¥18" is a **hard ceiling** on the housing. A brief can lower it, never raise it. |
| Packaging | PCB and battery are stacked (side by side they exceed 125 mm). With clearances, the thinnest possible housing is 25 mm, which is how the 18 mm brief is shown to be impossible. Internals weigh 92 g. |
| Cost | Moulds, side actions, machine rate and resin prices are in `domain/catalog.py`; tooling is spread over 2 years × 10k units. A plain housing costs about CNY 9.4. |
| Models | Live runs use the DeepSeek official API (`deepseek-v4-pro`, `deepseek-flash`); the optional `mixed` preset uses OpenAI `gpt-4.1-mini` as the judge. Prices are list prices, peak time, no cache hits. |
| "3D-ready" | A structured spec that passes the DFM rules. No CAD. |

## 2. Main decisions and trade-offs

1. **Models only where judgement is needed; everything with an exact answer is code.** The planner, constraint resolver, DFM critic, autofixer, cost model, gate and shortlist are plain code. LLMs read the brief, create the 2D concept, turn it into a 3D spec, make trade-offs in revisions, and score quality. *Gain:* exact, free, repeatable checks that a model cannot argue past. *Cost:* the rules must be written by hand.

2. **My own state machine instead of LangGraph; JSON-line traces instead of LangSmith.** The flow per variant is small and fixed, and I needed a step log keyed by inputs so that resume replays saved results and never pays twice for a model call. *Gain:* no framework to install, reliability code in plain view and tested, works offline. *Cost:* no visual tooling; I maintain the scheduler. `next_step` maps directly to LangGraph edges or a Temporal workflow if needed.

3. **One review-and-revise loop, only where it pays off.** DFM and cost loop at most 3 LLM rounds, with formula fixes applied first by code at zero tokens. Reading the brief, concept generation and judging are single passes: a misread number is fixed by a regex check, not by asking again, and polishing concepts toward a judge makes them all look alike.

4. **Exact resume through an idempotent step log.** Each step's result is stored under `H(run, variant, step, seq, inputs incl. prompt hash)`. Resume replays stored results through a pure reducer, so a run killed at variant 31,000 rebuilds the same state without new model calls. Only steps running at the moment of the crash are paid for again.

5. **Every model step has a code fallback, and fallbacks are never auto-approved.** Broken output is parsed, repaired (≤ 2 times), retried on the next model, then replaced by a code template. Anything built on degraded evidence (fallback, stale prices, local DFM) goes to a person with the reasons listed. This keeps the pipeline running without letting weak results through.

6. **Platform limits are never relaxed; impossible briefs are not fanned out.** Arithmetic finds briefs that cannot be met. A client limit is relaxed only through the Ask gate, only as far as needed, and never past the platform limit. If it still cannot be met, the brief gets a short report instead of thousands of failing variants. Requests the platform cannot make (metal, CNC) are flagged by code.

7. **Guards at every level against cascades and runaway cost.** Per call: timeouts, retries with backoff, a circuit breaker per model and tool, rate limiting. Per variant: step, revision and budget caps. Per run: a run budget and a breaker on infrastructure failures. Per brief: a quality breaker that pauses a brief whose variants keep failing the same way.

8. **The judge ranks; it does not decide alone.** It uses a different model from the generator, never drives revisions, and is checked against code-based ground truth, planted defects and repeat scoring. The live runs showed it is lenient, so code checks what it can (including what the 3D spec dropped from the concept), and a person signs off.

9. **A mock simulator by default, plus real runs.** The mock makes realistic mistakes so the loop, repair path and judge checks have something to catch, and the graded run needs no key. Mock numbers prove the machinery, not model quality; two live DeepSeek runs (about CNY 6.5–7 each) provide the real evidence.

## 3. What the live runs showed

- **Round 1** found seven problems the mock could not, all fixed with regression tests. The biggest: the per-variant budget had been set from mock numbers, so a third of variants fell back without any revision.
- **Round 2**, after the fixes, cut the fallback rate to 8%. It found two more problems, fixed offline: a variant was auto-approved after the reviser dropped the bumper it was designed around (the judge believed the change log), and the gate blocked for the wrong reason (the model called a feasible brief a "hard" conflict).

Details are in ARCHITECTURE §3.5 and `sample_outputs/reports_live/`.

## 4. Known limitations

- **Small live sample:** 2 configs × 12 variants per round. Enough to find real problems, not to measure rates precisely. The two round-2 fixes have not been measured live.
- **The intent checks are coarse.** Code checks that intent anchors map to existing features and that the concept's material, finish and decorations survive, but not geometry (a bumper that no longer stands proud would pass). The judge is the only check there, and it is lenient.
- **Single process, one SQLite file:** no worker leases or multi-tenant quotas.

## 5. With more time

1. Run a larger live eval, tune prompts per model, and build a golden set labelled by designers to calibrate the judge against people, not only rules.
2. Move to a durable workflow engine (Temporal) with worker leases, Postgres, OpenTelemetry export and dashboards for the SLOs in ARCHITECTURE §4.3.
3. Add a geometry kernel behind `dfm_validate`, and 2D concept images checked against the spec by a vision judge.
4. Cheaper revisions (JSON patches instead of whole specs), the provider batch API for batch runs, and a canary release flow tied to online signals.
