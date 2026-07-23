from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from scripts.adapter import grade
from scripts.diagnose import diagnose
from scripts.diagnosis_judge import judge_match
from scripts.metrics import TrialRecord, compute_metrics
from scripts.parser import ParserError, parse
from scripts.report import write_report
from scripts.solver_pipeline import run_pipeline

DEFAULT_CONCURRENCY = 3


def build_submission(statement: str, answer_text: str) -> str:
    """Marker format on purpose: it hits the parser's Layer-1 fast path, so
    the eval measures grading quality, not boundary-guessing ambiguity."""
    return f"Problem: {statement}\nMy answer: {answer_text}"


def _pipeline_trace_totals(trace_path: str) -> tuple[int, int]:
    """Sum (input, output) tokens across stages from a saved RunTrace file.
    Missing/corrupt file -> zeros; accounting must never crash a trial."""
    try:
        data = json.loads(Path(trace_path).read_text())
        tin = sum(v.get("in", 0) for v in data.get("tokens_by_stage", {}).values())
        tout = sum(v.get("out", 0) for v in data.get("tokens_by_stage", {}).values())
        return tin, tout
    except Exception:  # noqa: BLE001
        return 0, 0


async def _attach_diagnosis(client, rec: TrialRecord, *, parsed, reference,
                            comparison, injected_cause: str) -> None:
    """S5 + judge. A diagnosis failure must never kill the trial's verdict:
    it becomes diagnosis_error and the grade survives."""
    rec.diagnosis_attempted = True
    try:
        diag, tin, tout = await diagnose(client, parsed=parsed,
                                         reference=reference,
                                         comparison=comparison)
        rec.input_tokens += tin
        rec.output_tokens += tout
        rec.diagnosis_hypotheses = [h.misconception for h in diag.hypotheses]
        rec.diagnosis_explanations = [h.explanation for h in diag.hypotheses]
        rec.diagnosis_top = rec.diagnosis_hypotheses[0]
        verdict, tin2, tout2 = await judge_match(
            client, injected_cause=injected_cause, hypotheses=diag.hypotheses)
        rec.input_tokens += tin2
        rec.output_tokens += tout2
        rec.diagnosis_matched_rank = verdict.matched_rank
        rec.diagnosis_judge_reasoning = verdict.reasoning
    except Exception as e:  # noqa: BLE001 -- isolate: verdict already earned
        rec.diagnosis_error = f"{type(e).__name__}: {e}"


async def run_one_trial(client, sem: asyncio.Semaphore, *, problem: dict,
                        answer_text: str, trial_kind: str,
                        expected_match: bool,
                        injected_cause: Optional[str],
                        with_diagnosis: bool = False) -> TrialRecord:
    rec = TrialRecord(problem_id=problem["id"], domain=problem["domain"],
                      trial_kind=trial_kind, expected_match=expected_match,  # type: ignore[arg-type]
                      outcome="error", injected_cause=injected_cause)
    t0 = time.perf_counter()
    async with sem:
        try:
            outcome = await parse(client, build_submission(problem["statement"],
                                                           answer_text))
            parsed = outcome.parsed
            rec.input_tokens += outcome.trace.input_tokens
            rec.output_tokens += outcome.trace.output_tokens
            rec.parse_flags = list(parsed.ambiguity_flags)

            if not parsed.ready_for_pipeline():
                rec.outcome = "not_ready"
                return rec

            assert parsed.problem is not None
            result = await run_pipeline(client, parsed.problem,
                                        quarantine=parsed.student_answer)
            rec.pipeline_status = result.status
            rec.rounds_used = result.rounds_used
            rec.trace_path = result.trace_path
            tin, tout = _pipeline_trace_totals(result.trace_path)
            rec.input_tokens += tin
            rec.output_tokens += tout

            if result.blocked:
                rec.outcome = "uncertain"      # honest refusal, not a failure
                return rec

            assert result.reference is not None
            comparison = grade(parsed, result.reference)
            rec.outcome = "verdict"
            rec.verdict = comparison.verdict
            rec.is_match = comparison.is_match
            rec.method = comparison.method
            rec.detail = comparison.detail

            # S5: diagnose only CAUGHT wrong answers. Never CORRECT (nothing
            # to diagnose), never UNSUPPORTED (diagnosing the ungradeable is
            # guessing), never correct-answer trials (no ground truth).
            if (with_diagnosis and trial_kind == "wrong"
                    and comparison.verdict == "INCORRECT" and injected_cause):
                await _attach_diagnosis(client, rec, parsed=parsed,
                                        reference=result.reference,
                                        comparison=comparison,
                                        injected_cause=injected_cause)
            return rec

        except (ParserError, Exception) as e:  # noqa: BLE001 -- isolate the trial
            rec.outcome = "error"
            rec.error = f"{type(e).__name__}: {e}"
            return rec
        finally:
            rec.seconds = time.perf_counter() - t0


async def run_eval(client, bank: list[dict], *,
                   concurrency: int = DEFAULT_CONCURRENCY,
                   with_diagnosis: bool = False) -> list[TrialRecord]:
    sem = asyncio.Semaphore(concurrency)
    tasks = []
    for prob in bank:
        tasks.append(run_one_trial(
            client, sem, problem=prob,
            answer_text=prob["correct_answer"]["text"],
            trial_kind="correct", expected_match=True, injected_cause=None,
            with_diagnosis=with_diagnosis))
        for err in prob.get("injected_errors", []):
            tasks.append(run_one_trial(
                client, sem, problem=prob,
                answer_text=err["wrong_text"],
                trial_kind="wrong", expected_match=False,
                injected_cause=err["cause"], with_diagnosis=with_diagnosis))
    return list(await asyncio.gather(*tasks))


def load_bank(paths: list[str]) -> list[dict]:
    bank: list[dict] = []
    for p in paths:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{p}: bank must be a JSON list of problem objects")
        for prob in data:
            for key in ("id", "domain", "statement", "correct_answer"):
                if key not in prob:
                    raise ValueError(f"{p}: problem missing required key {key!r}: "
                                     f"{prob.get('id', '<no id>')}")
        bank.extend(data)
    return bank


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run the eval harness over a problem bank.")
    ap.add_argument("banks", nargs="+", help="bank JSON file(s)")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--limit", type=int, default=None,
                    help="run only the first N problems (cheap smoke run)")
    ap.add_argument("--out-dir", default="runs")
    args = ap.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from scripts.parser import PARSER_MODEL
    from scripts.solver_pipeline import AnthropicClient, K_SAMPLES, MODEL

    bank = load_bank(args.banks)
    if args.limit:
        bank = bank[:args.limit]
    n_trials = sum(1 + len(p.get("injected_errors", [])) for p in bank)
    print(f"{len(bank)} problems -> {n_trials} trials "
          f"(concurrency={args.concurrency}). This spends real tokens.")

    client = AnthropicClient()
    records = asyncio.run(run_eval(client, bank, concurrency=args.concurrency))
    metrics = compute_metrics(records)

    config = {"solver_model": MODEL, "parser_model": PARSER_MODEL,
              "k_samples": K_SAMPLES, "concurrency": args.concurrency,
              "banks": args.banks, "n_problems": len(bank)}
    report_path = write_report(records, metrics, config, out_dir=Path(args.out_dir))

    fmt = lambda x: "n/a" if x is None else f"{x:.0%}"
    print(f"\ncatch rate          : {fmt(metrics.catch_rate)}")
    print(f"false-positive rate : {fmt(metrics.false_positive_rate)}")
    print(f"uncertain rate      : {fmt(metrics.uncertain_rate)}")
    print(f"errors              : {metrics.n_error}")
    print(f"tokens (in/out)     : {metrics.total_input_tokens} / {metrics.total_output_tokens}")
    print(f"\nreport: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())