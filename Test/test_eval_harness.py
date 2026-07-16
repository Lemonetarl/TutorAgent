"""
test_eval_harness.py -- run:  python -m pytest Test/test_eval_harness.py -v
Metrics are proven on SYNTHETIC records first (zero tokens); then the whole
loop -- bank -> trials -> records -> report -- runs offline via MockClient.
"""
import asyncio
import json
from pathlib import Path

import pytest

from scripts.eval_harness import build_submission, load_bank, run_eval
from scripts.metrics import TrialRecord, compute_metrics
from scripts.report import render_report, write_report
from scripts.solver_pipeline import FakeMessage, MockClient, ToolUseBlock


# ------------------------- metrics: the scorer itself ----------------------

def rec(kind, outcome, is_match=None, seconds=1.0, tin=100, tout=50) -> TrialRecord:
    return TrialRecord(problem_id="p", domain="physics", trial_kind=kind,
                       expected_match=(kind == "correct"), outcome=outcome,
                       is_match=is_match, seconds=seconds,
                       input_tokens=tin, output_tokens=tout)


def test_catch_rate_counts_only_wrong_trials_with_verdicts():
    records = [rec("wrong", "verdict", is_match=False),   # caught
               rec("wrong", "verdict", is_match=True),    # missed (graded a match)
               rec("wrong", "uncertain")]                  # blocked -> EXCLUDED
    m = compute_metrics(records)
    assert m.catch_rate == 0.5           # 1 of 2 verdicts, not 1 of 3 trials


def test_false_positive_rate_on_correct_trials():
    records = [rec("correct", "verdict", is_match=True)] * 3 + \
              [rec("correct", "verdict", is_match=False)]     # the false alarm
    m = compute_metrics(records)
    assert m.false_positive_rate == 0.25


def test_blocked_trials_feed_uncertain_rate_not_the_other_rates():
    records = [rec("wrong", "uncertain"), rec("wrong", "verdict", is_match=False),
               rec("correct", "verdict", is_match=True)]
    m = compute_metrics(records)
    assert m.catch_rate == 1.0                    # the blocked one is excluded
    assert m.uncertain_rate == pytest.approx(1 / 3)
    assert m.uncertain_rate_wrong == 0.5
    assert m.uncertain_rate_correct == 0.0


def test_empty_denominators_are_None_not_zero():
    m = compute_metrics([rec("wrong", "uncertain")])
    assert m.catch_rate is None           # no wrong verdicts to judge
    assert m.false_positive_rate is None  # no correct trials at all
    # 'we have no data' must never look like 'we measured 0%'


def test_not_ready_and_error_counted_but_outside_pipeline_rates():
    records = [rec("correct", "not_ready"), rec("wrong", "error"),
               rec("wrong", "verdict", is_match=False)]
    m = compute_metrics(records)
    assert m.n_not_ready == 1 and m.n_error == 1
    assert m.uncertain_rate == 0.0        # only the verdict trial entered


# ---------------- the whole loop, offline, against MockClient --------------

MINI_BANK = [{
    "id": "phys_free_fall_45m", "domain": "physics",
    "statement": "A ball is dropped from rest from a height of 45 m. Taking "
                 "g = 9.8 m/s**2, how long does it take to reach the ground?",
    "correct_answer": {"text": "t = 3.03 s"},
    "injected_errors": [
        {"wrong_text": "t = 2.14 s",
         "cause": "forgot the factor of 2: used sqrt(h/g)"}],
}]


def parse_msg(answer_text: str, sympy_form: str) -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="tp", name="emit_parse", input={
        "reasoning": "markers", 
        "problem": {"subject_domain": "physics",
                    "statement": "A ball is dropped from rest from a height of "
                                 "45 m. Taking g = 9.8 m/s**2, how long does it "
                                 "take to reach the ground?",
                    "is_complete": True, "has_multiple_parts": False},
        "target_quantity": "time to reach the ground",
        "student_answer": {"answer_text": answer_text,
                           "values": [{"variable": "t", "sympy_form": sympy_form}],
                           "units": "s", "shows_work": False},
        "ambiguity_flags": [], "ambiguity_notes": "", "confidence": "high"})])


def solver_msg() -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="ts", name="emit_solution", input={
        "given": ["h = 45 m", "g = 9.8 m/s**2"], "unknowns": ["t"],
        "method_name": "kinematics",
        "steps": [{"id": 1, "action": "solve", "equation": "sqrt(2*h/g)",
                   "result": "3.0304"}],
        "final_answer": {"value": "3.0304", "unit": "s",
                         "symbolic_form": "sqrt(2*h/g)", "answer_type": "scalar",
                         "additional_values": []}})])


def verifier_msgs() -> list[FakeMessage]:
    return [
        FakeMessage(content=[ToolUseBlock(id="tv1", name="evaluate_numeric",
            input={"expr": "sqrt(2*h/g)", "substitutions": {"h": 45, "g": 9.8}})]),
        FakeMessage(content=[ToolUseBlock(id="tv2", name="submit_solution",
            input=solver_msg().content[0].input)]),
    ]


def test_full_loop_offline():
    # 2 trials x (parse + 1 solver [k=1] + 2 verifier msgs) = 8 scripted messages.
    # concurrency=1 so the shared script is consumed in deterministic order.
    script = ([parse_msg("t = 3.03 s", "3.03")] + [solver_msg()] + verifier_msgs()
              + [parse_msg("t = 2.14 s", "2.14")] + [solver_msg()] + verifier_msgs())
    client = MockClient(script)
    records = asyncio.run(run_eval(client, MINI_BANK, concurrency=1))

    assert len(records) == 2
    correct, wrong = records
    assert correct.trial_kind == "correct" and correct.outcome == "verdict"
    assert correct.is_match is True                 # 3.03 vs 3.0304: within 1%
    assert wrong.trial_kind == "wrong" and wrong.outcome == "verdict"
    assert wrong.is_match is False                  # 2.14: caught
    #assert wrong.injected_cause.startswith("forgot the factor of 2")
    assert correct.input_tokens > 0 and correct.seconds > 0

    m = compute_metrics(records)
    assert m.catch_rate == 1.0 and m.false_positive_rate == 0.0

    report = render_report(records, m, {"solver_model": "mock", "k_samples": 1,
                                        "parser_model": "mock", "concurrency": 1,
                                        "banks": ["mini"]})
    assert "catch rate" in report and "100%" in report
    assert "phys_free_fall_45m" in report


def test_report_files_written(tmp_path: Path):
    records = [rec("wrong", "verdict", is_match=False)]
    m = compute_metrics(records)
    path = write_report(records, m, {"banks": ["x"]}, out_dir=tmp_path)
    assert path.exists() and path.suffix == ".md"
    jsonls = list(tmp_path.glob("eval_records_*.jsonl"))
    assert len(jsonls) == 1
    row = json.loads(jsonls[0].read_text().splitlines()[0])
    assert row["trial_kind"] == "wrong" and row["outcome"] == "verdict"


def test_bank_loader_rejects_malformed_banks(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"id": "x", "domain": "math"}]))  # no statement
    with pytest.raises(ValueError, match="missing required key"):
        load_bank([str(bad)])


def test_submission_hits_the_fast_path():
    from scripts.parser import split_fast_path
    sub = build_submission("A ball is dropped from 45 m. How long?", "t = 3 s")
    assert split_fast_path(sub) is not None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
