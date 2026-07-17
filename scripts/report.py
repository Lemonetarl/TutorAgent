"""
report.py -- presentation only. Records + metrics in, markdown out.
No API calls, no scoring logic (that's metrics.py's job -- a report that
recomputes numbers is a report that can disagree with the scorer).
Also writes the raw records as JSONL so everything here can be regenerated
later without re-running a single trial.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from scripts.metrics import EvalMetrics, TrialRecord


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def render_report(records: list[TrialRecord], metrics: EvalMetrics,
                  config: dict) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    add = lines.append

    add(f"# Eval report -- {ts}")
    add("")
    add(f"Config: solver=`{config.get('solver_model')}` (k={config.get('k_samples')}), "
        f"parser=`{config.get('parser_model')}`, concurrency={config.get('concurrency')}, "
        f"banks={config.get('banks')}")
    add("")

    add("## Headline metrics")
    add("")
    add("| metric | value | definition |")
    add("|---|---|---|")
    add(f"| **catch rate** | **{_pct(metrics.catch_rate)}** | of wrong answers that "
        f"got a verdict, fraction flagged INCORRECT |")
    add(f"| **false-positive rate** | **{_pct(metrics.false_positive_rate)}** | of "
        f"correct answers that got a verdict, fraction wrongly flagged |")
    add(f"| uncertain rate | {_pct(metrics.uncertain_rate)} | of pipeline entries, "
        f"fraction that BLOCKED (honest refusal, excluded from the rates above) |")
    add(f"| uncertain (correct / wrong) | {_pct(metrics.uncertain_rate_correct)} / "
        f"{_pct(metrics.uncertain_rate_wrong)} | uncertainty split by trial kind |")
    add(f"| **diagnosis top-1 accuracy** | **{_pct(metrics.diagnosis_top1_accuracy)}** | "
        f"of caught wrong answers diagnosed (n={metrics.n_diagnosed}), the TOP "
        f"hypothesis matched the injected cause (semantic judge) |")
    add(f"| diagnosis any-hypothesis accuracy | {_pct(metrics.diagnosis_any_accuracy)} | "
        f"any of the <=3 ranked hypotheses matched the injected cause |")
    add("")

    add("## Outcome breakdown")
    add("")
    add(f"- trials: {metrics.n_trials} ({metrics.n_correct_trials} correct-answer, "
        f"{metrics.n_wrong_trials} injected-wrong)")
    add(f"- verdicts: {metrics.n_verdict} | uncertain: {metrics.n_uncertain} | "
        f"not pipeline-ready: {metrics.n_not_ready} | errors: {metrics.n_error}")
    add("")

    add("## Cost and latency")
    add("")
    add(f"- tokens: {metrics.total_input_tokens} in / {metrics.total_output_tokens} out")
    add(f"- wall time: {metrics.total_seconds:.1f}s total"
        + (f", {metrics.mean_seconds_per_trial:.1f}s mean per trial"
           if metrics.mean_seconds_per_trial is not None else ""))
    add("- caveat: wall time overlaps under concurrency; per-trial seconds are "
        "honest, the total is not a sum of a serial run")
    add("")

    blocked = [r for r in records if r.outcome == "uncertain"]
    if blocked:
        add("## Exemplars: honest refusals (Invariant 3 on camera)")
        add("")
        add("Trials where the pipeline declined to issue a verdict rather than "
            "guess. With k=1, these are the runs where the blind verifier -- now "
            "the load-bearing check -- disagreed with the solver to the end:")
        add("")
        for r in blocked:
            add(f"- `{r.problem_id}` ({r.trial_kind}"
                + (f", injected: {r.injected_cause}" if r.injected_cause else "")
                + f") -> trace `{r.trace_path}`")
        add("")

    add("## Per-trial results")
    add("")
    add("| problem | kind | outcome | verdict | injected cause | top diagnosis | tok in/out | s |")
    add("|---|---|---|---|---|---|---|---|")
    for r in records:
        if r.diagnosis_error:
            diag = "(diagnosis errored)"
        elif r.diagnosis_attempted:
            hit = (f" (hit@{r.diagnosis_matched_rank})"
                   if r.diagnosis_matched_rank is not None else " (miss)")
            diag = f"{r.diagnosis_top[:60]}{hit}"
        else:
            diag = ""
        add(f"| {r.problem_id} | {r.trial_kind} | {r.outcome} | {r.verdict or ''} "
            f"| {r.injected_cause or ''} | {diag} "
            f"| {r.input_tokens}/{r.output_tokens} | {r.seconds:.1f} |")
    add("")

    errors = [r for r in records if r.outcome == "error"]
    if errors:
        add("## Errors (crashed trials, isolated)")
        add("")
        for r in errors:
            add(f"- `{r.problem_id}` ({r.trial_kind}): {r.error}")
        add("")

    add("## Caveats")
    add("")
    add("- Verdicts near the 1% physics tolerance boundary (e.g. 3 vs 3.03 s) are "
        "policy calls, not math errors; see DEVLOG.")
    add("- Token counts run ~30% above pre-migration estimates (new tokenizer).")
    add("- Adaptive thinking tokens count against max_tokens and vary per problem, "
        "so per-trial cost is not uniform.")
    add("- Diagnosis matching is judged SEMANTICALLY by a small model (a model "
        "grading a model); hypotheses and causes are in the records JSONL -- "
        "spot-check them. Replay verification (mechanically reproducing the "
        "student's value from the hypothesized error) is the planned v2 upgrade.")
    return "\n".join(lines)


def write_report(records: list[TrialRecord], metrics: EvalMetrics, config: dict,
                 out_dir: Path = Path("runs")) -> Path:
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"eval_report_{stamp}.md"
    report_path.write_text(render_report(records, metrics, config),
                           encoding="utf-8")
    records_path = out_dir / f"eval_records_{stamp}.jsonl"
    with records_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.to_dict()) + "\n")
    return report_path