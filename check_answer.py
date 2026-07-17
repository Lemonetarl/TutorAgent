"""
check_answer.py -- check one submission end to end, with real Claude calls.

    python check_answer.py                                   # interactive
    python check_answer.py "Problem: ... My answer: ..."     # one raw string
    python check_answer.py --problem "..." --answer "..."    # separate fields
    python check_answer.py --file submission.txt

THE FOUR HONEST OUTCOMES (all of them are voiced, none guessed away):
  CORRECT / EQUIVALENT  the answer matches the independently verified solution
  INCORRECT             it doesn't -- followed by RANKED HYPOTHESES about the
                        specific mistake (inferred, not proven; labeled so)
  UNSUPPORTED           the comparator cannot judge this answer type/format --
                        says so instead of pretending
  UNCERTAIN             solver and verifier never agreed; the system declines
                        to issue any verdict and routes to human review

An answer is optional: a problem alone gets independently solved and the
verified solution is shown (tutoring mode, no grade).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from scripts.adapter import grade
from scripts.diagnose import diagnose
from scripts.eval_harness import _pipeline_trace_totals  # deliberate reuse
from scripts.parser import ParserError, parse
from scripts.solver_pipeline import run_pipeline

BAR = "=" * 60


def _print_reference(reference) -> None:
    vals = ", ".join(v.sympy_form for v in reference.values)
    units = f" {reference.units}" if reference.units else ""
    print(f"  verified answer : {vals}{units}   ({reference.answer_type})")


async def check(client, raw_text: str) -> dict:
    """Runs the whole flow, prints as it goes, returns a small summary dict
    (handy for tests and for the future Gradio wrapper)."""
    summary: dict = {"stage": "parse", "verdict": None, "status": None,
                     "diagnosed": False}

    # ---- S1: parse -------------------------------------------------------
    try:
        outcome = await parse(client, raw_text)
    except ParserError as e:
        print(f"\nCould not read that submission: {e}")
        print("Please restate the problem (and your answer, if any) more plainly.")
        summary["stage"] = "parse_failed"
        return summary
    parsed = outcome.parsed
    print(f"\n[parse] confidence={parsed.confidence}"
          + (f"  flags={parsed.ambiguity_flags}" if parsed.ambiguity_flags else ""))

    if not parsed.ready_for_pipeline():
        print(f"\nThis can't be graded safely as written: "
              f"{parsed.ambiguity_notes or 'low parse confidence'}")
        print("Rather than guess, please restate or split the problem.")
        summary["stage"] = "not_ready"
        return summary

    # ---- S2/S3/S3b: independently solve + verify -------------------------
    assert parsed.problem is not None
    result = await run_pipeline(client, parsed.problem,
                                quarantine=parsed.student_answer)
    summary["status"] = result.status
    print(f"[solve] status={result.status}  rounds={result.rounds_used}"
          + (f"  verifier method: {result.verifier_method}"
             if result.verifier_method else ""))

    if result.blocked:
        print(f"\n{BAR}\n  NO VERDICT -- the independent solver and verifier "
              f"could not agree.\n  Telling you that you're wrong on shaky "
              f"grounds would be worse than\n  saying nothing: this case was "
              f"routed to human review instead.\n{BAR}")
        summary["stage"] = "uncertain"
        return summary

    assert result.reference is not None

    # ---- no answer submitted: tutoring mode, not verdict mode ------------
    if not parsed.has_gradeable_answer():
        print(f"\n{BAR}\n  No answer was submitted, so there is nothing to "
              f"grade.\n  The independently verified solution is:\n")
        _print_reference(result.reference)
        print(BAR)
        summary["stage"] = "solution_only"
        return summary

    # ---- S4: grade -------------------------------------------------------
    comparison = grade(parsed, result.reference)
    summary["stage"] = "graded"
    summary["verdict"] = comparison.verdict
    print(f"\n{BAR}\n  VERDICT: {comparison.verdict}"
          + (f"   ({comparison.detail})" if comparison.detail else "")
          + f"\n{BAR}")
    _print_reference(result.reference)

    if comparison.verdict == "CORRECT":
        print("  Your answer matches the verified solution. Nice work.")
    elif comparison.verdict == "EQUIVALENT":
        print("  Correct -- your answer is mathematically equivalent to the "
              "verified\n  solution, just written in a different form.")
        if comparison.issues:
            print(f"  note: {', '.join(comparison.issues)}")
    elif comparison.verdict == "UNSUPPORTED":
        print(f"  This answer couldn't be graded: "
              f"{comparison.detail or comparison.method}.")
        print("  No verdict, and no guessing at mistakes for an answer we "
              "couldn't judge.")

    # ---- S5: diagnosis, only for a confirmed INCORRECT -------------------
    if comparison.verdict == "INCORRECT":
        print("\n  Likely mistake -- ranked hypotheses (inferred from your "
              "answer,\n  not proven):")
        try:
            diag, _, _ = await diagnose(client, parsed=parsed,
                                        reference=result.reference,
                                        comparison=comparison)
            for i, h in enumerate(diag.hypotheses, start=1):
                print(f"\n   {i}. {h.misconception}")
                print(f"      {h.explanation}")
            summary["diagnosed"] = True
        except Exception as e:  # noqa: BLE001 -- the verdict must survive
            print(f"\n   A specific mistake couldn't be identified for this "
                  f"answer\n   ({type(e).__name__}). The verdict above still "
                  f"stands.")

    # ---- accounting ------------------------------------------------------
    tin, tout = _pipeline_trace_totals(result.trace_path)
    tin += outcome.trace.input_tokens
    tout += outcome.trace.output_tokens
    print(f"\n  [tokens {tin} in / {tout} out  ·  trace {result.trace_path}]")
    return summary


def _gather_raw_text(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.problem:
        raw = f"Problem: {args.problem}"
        if args.answer:
            raw += f"\nMy answer: {args.answer}"
        return raw
    if args.raw:
        return args.raw
    # interactive: two fields = the Layer-1 fast path, guaranteed
    print("Enter the problem (one line):")
    problem = input("> ").strip()
    print("Enter your answer (press Enter to skip and just see the solution):")
    answer = input("> ").strip()
    raw = f"Problem: {problem}"
    if answer:
        raw += f"\nMy answer: {answer}"
    return raw


def main() -> int:
    ap = argparse.ArgumentParser(description="Check one submission end to end.")
    ap.add_argument("raw", nargs="?", help="full raw submission text")
    ap.add_argument("--problem", help="problem statement (separate field)")
    ap.add_argument("--answer", help="your answer (separate field, optional)")
    ap.add_argument("--file", help="read the submission from a file")
    args = ap.parse_args()

    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set -- put it in .env")
    from scripts.solver_pipeline import AnthropicClient  # after load_dotenv

    raw_text = _gather_raw_text(args)
    if not raw_text.strip():
        raise SystemExit("empty submission")

    asyncio.run(check(AnthropicClient(), raw_text))
    return 0


if __name__ == "__main__":
    sys.exit(main())

