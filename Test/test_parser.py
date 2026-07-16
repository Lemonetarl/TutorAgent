"""
test_parser.py -- run from the project root:
    python -m pytest TestScripts/test_parser.py -v

Flow tests script ONLY the Haiku messages (via the same MockClient the
pipeline tests use); the fast path, retry loop, and every Layer-3 validator
run for real. Validator tests need no mock at all.
"""
import asyncio
from typing import Literal, Optional

import pytest

from scripts.models import AnswerValue, ParsedInput, ProblemOnly, StudentAnswer
from scripts.parser import ParserError, parse, split_fast_path
from scripts.solver_pipeline import FakeMessage, MockClient, ToolUseBlock
from scripts.validators import (run_validators, validate_answer_verbatim,
                                validate_completeness, validate_leakage,
                                validate_reconstruction, validate_values_parse)

RAW_MARKED = ("Problem: A car accelerates from rest at 3 m/s**2 for 5 s. "
              "Find its final speed. Answer: v = 15 m/s")
RAW_UNMARKED = ("a ball is dropped from 45 m how long until it hits "
                "the ground i think its 3 seconds")


def payload(statement: str = "A car accelerates from rest at 3 m/s**2 for 5 s. Find its final speed.",
            answer_text: str = "v = 15 m/s",
            sympy_form: str = "15",
            variable: Optional[str] = "v",          # None is legal: a bare quantity
            units: Optional[str] = "m/s",
            confidence: Literal["high", "medium", "low"] = "high",
            target: str = "the car's final speed") -> dict:
    return {"reasoning": "test", 
            "problem": {"subject_domain": "physics", "statement": statement,
                        "is_complete": True, "has_multiple_parts": False},
            "target_quantity": target,
            "student_answer": {"answer_text": answer_text,
                               "values": [{"variable": variable,
                                           "sympy_form": sympy_form}],
                               "units": units, "shows_work": False},
            "ambiguity_flags": [], "ambiguity_notes": "",
            "confidence": confidence}


def parse_msg(p: dict) -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="tu_p", name="emit_parse", input=p)])


def run(client, raw):
    return asyncio.run(parse(client, raw))


# ------------------------------- flow tests --------------------------------

def test_clean_parse_first_attempt():
    client = MockClient([parse_msg(payload())])
    out = run(client, RAW_MARKED)
    assert out.attempts == 1 and out.fast_path_used
    assert out.parsed.confidence == "high"
    assert out.parsed.ready_for_pipeline() and out.parsed.has_gradeable_answer()
    # has_gradeable_answer() guarantees this at runtime, but the checker cannot
    # see through a method call -- assert makes the narrowing explicit to both.
    assert out.parsed.student_answer is not None
    assert out.parsed.student_answer.values[0].sympy_form == "15"
    # the Layer-1 hint reached the model
    assert "PRE-SPLIT HINT" in client.calls[0]["messages"][0]["content"]
    # and the call was routed to Haiku at temperature 0
    assert "haiku" in client.calls[0]["model"]
    assert client.calls[0]["temperature"] == 0.0


def test_validation_error_triggers_one_retry_with_error_feedback():
    bad = payload(sympy_form="v=15")            # '=' violates _no_equals
    good = payload()
    client = MockClient([parse_msg(bad), parse_msg(good)])
    out = run(client, RAW_MARKED)
    assert out.attempts == 2
    retry_prompt = client.calls[1]["messages"][0]["content"]
    assert "FAILED VALIDATION" in retry_prompt
    assert "sympy_form" in retry_prompt         # the exact error crossed over


def test_two_failures_raise_parser_error_not_a_guess():
    bad = payload(sympy_form="v=15")
    client = MockClient([parse_msg(bad), parse_msg(bad)])
    with pytest.raises(ParserError, match="after 2 attempts"):
        run(client, RAW_MARKED)


def test_unmarked_input_skips_fast_path():
    p = payload(
        statement="A ball is dropped from 45 m. How long until it hits the ground?",
        answer_text="i think its 3 seconds", sympy_form="3", variable=None,
        units="seconds", confidence="medium",
        target="the time for the ball to reach the ground")
    client = MockClient([parse_msg(p)])
    out = run(client, RAW_UNMARKED)
    assert not out.fast_path_used
    assert "PRE-SPLIT HINT" not in client.calls[0]["messages"][0]["content"]


# ----------------------- Layer-3 validators, directly -----------------------

def _mk(statement: str, answer_text: str, sympy_form: str,
        target: str = "the final speed",
        units: Optional[str] = None,
        confidence: Literal["high", "medium", "low"] = "high") -> ParsedInput:
    return ParsedInput(
        reasoning="t",
        problem=ProblemOnly(subject_domain="physics", statement=statement,
                            is_complete=True, has_multiple_parts=False),
        target_quantity=target,
        student_answer=StudentAnswer(
            answer_text=answer_text,
            values=[AnswerValue(sympy_form=sympy_form)], units=units),
        confidence=confidence)


def test_leakage_is_flagged_and_downgraded():
    raw = "A ball falls 20 m. How long? I got 2.02 s"
    parsed = _mk("A ball falls 20 m. How long? I got 2.02 s",
                 "I got 2.02 s", "2.02", target="time to fall", units="s")
    out = run_validators(raw, parsed)
    assert any(f.flag == "problem_answer_boundary_unclear" and
               "leakage" in f.note for f in out)


def test_leakage_token_boundary_no_false_positive():
    # student value '2.0' must NOT fire inside the statement's '2.02'
    parsed = _mk("A process takes 2.02 s per cycle. How many cycles fit in a minute?",
                 "about 29", "2.0", target="number of cycles")
    assert validate_leakage("irrelevant", parsed) == []


def test_paraphrased_answer_text_is_caught():
    raw = "find x if 2x=6. i wrote down three"
    parsed = _mk("Find x if 2x = 6.", "the student answered three", "3",
                 target="x")
    out = validate_answer_verbatim(raw, parsed)
    assert out and out[0].downgrade_to == "low"


def test_dropped_content_is_caught_by_coverage():
    raw = ("A projectile is launched at 30 degrees above the horizontal with "
           "an initial speed of 20 meters per second from a cliff of height "
           "45 meters. Determine the total time of flight. I got 5.1 s")
    parsed = _mk("Find the time.", "I got 5.1 s", "5.1",
                 target="time of flight", units="s")
    out = validate_reconstruction(raw, parsed)
    assert out and out[0].flag == "input_garbled" and out[0].downgrade_to == "low"


def test_statement_that_never_asks_anything():
    parsed = _mk("The ball is red and the table is heavy.", "answer 4", "4",
                 target="")
    flags = {f.note for f in validate_completeness("x", parsed)}
    assert any("never asks" in n for n in flags)
    assert any("target_quantity is empty" in n for n in flags)


def test_unparseable_sympy_form_is_flagged():
    parsed = _mk("Differentiate exp(3*x). What is the derivative?",
                 "3exp(3x)", "3*exp(3x)", target="the derivative")
    out = validate_values_parse("x", parsed)
    assert out and out[0].flag == "values_unclear"
    assert "exp(3*x)" in out[0].note            # the hint teaches the fix


def test_findings_actually_downgrade_through_full_parse():
    # end-to-end: model emits a leaked parse at HIGH; Layer 3 pulls it to MEDIUM
    leaked = payload(
        statement="A ball falls 20 m. How long? I got 2.02 s",
        answer_text="I got 2.02 s", sympy_form="2.02", variable=None,
        units="s", target="time to fall")
    client = MockClient([parse_msg(leaked)])
    out = run(client, "A ball falls 20 m. How long? I got 2.02 s")
    assert out.parsed.confidence == "medium"
    assert "problem_answer_boundary_unclear" in out.parsed.ambiguity_flags
    assert "leakage" in out.parsed.ambiguity_notes


# ------------------------------ the fast path -------------------------------

def test_fast_path_detects_markers():
    got = split_fast_path(RAW_MARKED)
    assert got is not None
    problem_half, answer_half = got
    assert answer_half is not None
    assert "final speed" in problem_half and "15 m/s" in answer_half

def test_fast_path_problem_only():
    got = split_fast_path("Problem: Solve x**2 - 4 = 0.")
    assert got is not None and got[1] is None

def test_fast_path_declines_unmarked_text():
    assert split_fast_path(RAW_UNMARKED) is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))