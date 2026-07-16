"""
test_real_api_e2e.py -- the FIRST real Claude call through the whole pipeline.
Run:  python -m pytest Test/test_real_api_e2e.py -v -s
(-s shows the printed trace; this test spends real tokens.)
"""
FLOW_DIAGRAM = """
raw text ──parser.parse()──► ParseOutcome
                                  │  parsed.ready_for_pipeline()?  no → ask the student
                                  ▼
        run_pipeline(client, parsed.problem, quarantine=parsed.student_answer)
                                  │  result.blocked?  yes → human_review_queue, no verdict
                                  ▼
        grade(parsed, result.reference) ──► CORRECT / EQUIVALENT / INCORRECT / UNSUPPORTED
"""

import asyncio
import os

import pytest
from dotenv import load_dotenv

from scripts.adapter import grade
from scripts.models import ParsedInput
from scripts.parser import parse
from scripts.solver_pipeline import AnthropicClient, run_pipeline

load_dotenv()

raw_text = ("Problem: A ball is dropped from 45 m. How long until it hits "
           "the ground? My answer: I think it's 3 seconds.")


def api_client() -> AnthropicClient:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise ValueError("ANTHROPIC_API_KEY is not set in the environment variables")
    return AnthropicClient()


async def _run_flow(client: AnthropicClient, raw_text: str):
    """The whole pipeline in ONE event loop: the client's underlying httpx
    connections must not be reused across separate asyncio.run() calls.
    Returns (parsed, result, comparison); the latter two are None when the
    flow legitimately stops early (not pipeline-ready, or UNCERTAIN)."""
    outcome = await parse(client, raw_text)
    parsed = outcome.parsed                       # parse() returns ParseOutcome, not ParsedInput
    assert isinstance(parsed, ParsedInput)
    print(f"\n[parse] confidence={parsed.confidence} flags={parsed.ambiguity_flags} "
         f"fast_path={outcome.fast_path_used}")

    if not parsed.ready_for_pipeline():
        print(f"[parse] NOT ready for pipeline: {parsed.ambiguity_notes}")
        return parsed, None, None

    assert parsed.problem is not None             # narrows Optional[ProblemOnly]
    result = await run_pipeline(client, parsed.problem, quarantine=parsed.student_answer)
    print(f"[pipeline] status={result.status} blocked={result.blocked} "
         f"rounds={result.rounds_used} votes={result.solver_votes}/{result.solver_k} "
         f"trace={result.trace_path}")

    # Do NOT assert reference here: a blocked (UNCERTAIN) result has
    # reference=None BY DESIGN. Check blocked first, then narrow.
    if result.blocked:
        print("[pipeline] UNCERTAIN -- routed to human_review_queue.jsonl, no verdict")
        return parsed, result, None

    assert result.reference is not None           # guaranteed whenever blocked is False
    comparison = grade(parsed, result.reference)
    print(f"[grade] verdict={comparison.verdict} method={comparison.method} "
         f"detail={comparison.detail!r}")
    return parsed, result, comparison


def test_real_end_to_end():
    client = api_client()
    parsed, result, comparison = asyncio.run(_run_flow(client, raw_text))

    # Branch on the outcome -- do NOT assert the happy path. not-ready and
    # UNCERTAIN are legitimate, informative results for a first real run, not
    # failures. Skip (with a reason) so pytest records them as inspected, not
    # broken.
    if not parsed.ready_for_pipeline():
        pytest.skip(f"parse not pipeline-ready ({parsed.ambiguity_notes}) -- "
                   f"inspect the parse, this is a real outcome not a bug")
    assert result is not None
    if result.blocked:
        pytest.skip("pipeline returned UNCERTAIN -- inspect the trace file and "
                   "human_review_queue.jsonl; a real, informative outcome")

    # Grading produced a verdict. Do NOT hardcode which one: 45 m free-fall
    # gives t ~= 3.03 s, and the student's "3" sits within ~1% of it -- right
    # at the physics tolerance boundary, so CORRECT / EQUIVALENT / INCORRECT
    # are all legitimate depending on the model's exact g and rounding.
    assert comparison is not None
    assert comparison.verdict in ("CORRECT", "EQUIVALENT", "INCORRECT", "UNSUPPORTED")
    print(f"\n>>> FIRST REAL END-TO-END VERDICT: {comparison.verdict} "
         f"(via {comparison.method}) <<<")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "-s", "--tb=short"]))