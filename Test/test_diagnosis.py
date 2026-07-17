"""
test_diagnosis.py -- run:  python -m pytest Test/test_diagnosis.py -v
Accuracy metrics proven on synthetic records first (zero tokens); then the
full loop WITH diagnosis runs offline via MockClient: parse -> solve ->
verify -> grade -> diagnose -> judge, everything real except the LLM words.
"""
import asyncio

import pytest

from scripts.eval_harness import run_eval
from scripts.metrics import TrialRecord, compute_metrics
from scripts.solver_pipeline import FakeMessage, MockClient, ToolUseBlock


# ------------------- the accuracy metrics, on synthetic records -------------

def drec(matched_rank=None, attempted=True, error="") -> TrialRecord:
    return TrialRecord(problem_id="p", domain="math", trial_kind="wrong",
                       expected_match=False, outcome="verdict",
                       verdict="INCORRECT", is_match=False,
                       diagnosis_attempted=attempted,
                       diagnosis_top="x" if attempted else "",
                       diagnosis_matched_rank=matched_rank,
                       diagnosis_error=error)


def test_top1_and_any_accuracy():
    records = [drec(matched_rank=1),      # top hit
               drec(matched_rank=2),      # hit, but not top
               drec(matched_rank=None)]   # judged miss -- stays in denominator
    m = compute_metrics(records)
    assert m.n_diagnosed == 3
    assert m.diagnosis_top1_accuracy == pytest.approx(1 / 3)
    assert m.diagnosis_any_accuracy == pytest.approx(2 / 3)


def test_errored_diagnosis_leaves_denominator_but_unattempted_never_enters():
    records = [drec(matched_rank=1),
               drec(error="ValueError: boom"),   # crashed -> excluded
               drec(attempted=False)]            # never ran -> excluded
    m = compute_metrics(records)
    assert m.n_diagnosed == 1
    assert m.diagnosis_top1_accuracy == 1.0


def test_no_diagnosed_trials_is_None_not_zero():
    m = compute_metrics([drec(attempted=False)])
    assert m.diagnosis_top1_accuracy is None
    assert m.diagnosis_any_accuracy is None


# --------------- the whole loop with diagnosis, offline ---------------------

BANK = [{
    "id": "ode_sign", "domain": "math",
    "statement": "Solve dy/dx = -3*y with y(0) = 1.",
    "correct_answer": {"text": "exp(-3*x)"},
    "injected_errors": [
        {"wrong_text": "exp(3*x)", "cause": "Dropping sign during calculation"}],
}]


def parse_msg(ans: str) -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="tp", name="emit_parse", input={
        "reasoning": "x",
        "problem": {"subject_domain": "math",
                    "statement": "Solve dy/dx = -3*y with y(0) = 1.",
                    "is_complete": True, "has_multiple_parts": False},
        "target_quantity": "the solution y(x)",
        "student_answer": {"answer_text": ans,
                           "values": [{"sympy_form": ans}],
                           "units": None, "shows_work": False},
        "ambiguity_flags": [], "ambiguity_notes": "", "confidence": "high"})])


def solver_msg() -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="ts", name="emit_solution", input={
        "given": ["dy/dx = -3y", "y(0) = 1"], "unknowns": ["y"],
        "method_name": "separation_of_variables",
        "steps": [{"id": 1, "action": "solve", "equation": "exp(-3*x)",
                   "result": "exp(-3*x)"}],
        "final_answer": {"value": "exp(-3*x)", "unit": None,
                         "symbolic_form": "exp(-3*x)", "answer_type": "symbolic",
                         "additional_values": []}})])


def verifier_msgs() -> list[FakeMessage]:
    return [FakeMessage(content=[ToolUseBlock(id="v1",
                name="check_symbolic_equivalence",
                input={"expr_a": "exp(-3*x)", "expr_b": "exp(-3*x)"})]),
            FakeMessage(content=[ToolUseBlock(id="v2", name="submit_solution",
                input=solver_msg().content[0].input)])]


def diagnose_msg() -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="td", name="emit_diagnosis", input={
        "hypotheses": [
            {"misconception": "sign error in the exponent",
             "explanation": "integrating dy/y = -3 dx but writing +3x gives exp(3x)"},
            {"misconception": "solved dy/dx = +3y instead",
             "explanation": "misread the equation's sign"}]})])


def judge_msg(rank) -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="tj", name="emit_match", input={
        "matched_rank": rank, "reasoning": "same mechanism: dropped sign"})])


def test_full_loop_with_diagnosis_offline():
    # correct trial: parse+solve+2 verifier = 4 msgs (no diagnosis)
    # wrong trial:   parse+solve+2 verifier + diagnose + judge = 6 msgs
    script = ([parse_msg("exp(-3*x)")] + [solver_msg()] + verifier_msgs()
              + [parse_msg("exp(3*x)")] + [solver_msg()] + verifier_msgs()
              + [diagnose_msg(), judge_msg(1)])
    client = MockClient(script)
    records = asyncio.run(run_eval(client, BANK, concurrency=1,
                                   with_diagnosis=True))
    correct, wrong = records

    assert correct.diagnosis_attempted is False        # never diagnose correct
    assert wrong.outcome == "verdict" and wrong.is_match is False
    assert wrong.diagnosis_attempted is True
    assert wrong.diagnosis_top == "sign error in the exponent"
    assert wrong.diagnosis_matched_rank == 1

    m = compute_metrics(records)
    assert m.n_diagnosed == 1
    assert m.diagnosis_top1_accuracy == 1.0
    assert m.catch_rate == 1.0 and m.false_positive_rate == 0.0


def test_out_of_range_judge_rank_is_clamped_to_miss():
    script = ([parse_msg("exp(-3*x)")] + [solver_msg()] + verifier_msgs()
              + [parse_msg("exp(3*x)")] + [solver_msg()] + verifier_msgs()
              + [diagnose_msg(), judge_msg(7)])       # hallucinated rank
    records = asyncio.run(run_eval(MockClient(script), BANK, concurrency=1,
                                   with_diagnosis=True))
    wrong = records[1]
    assert wrong.diagnosis_matched_rank is None       # clamped, not a fake hit
    m = compute_metrics(records)
    assert m.diagnosis_top1_accuracy == 0.0


def test_diagnosis_crash_never_kills_the_verdict():
    # script ENDS before the diagnose message: S5's call explodes on an
    # exhausted MockClient, but the trial's grade must survive.
    script = ([parse_msg("exp(-3*x)")] + [solver_msg()] + verifier_msgs()
              + [parse_msg("exp(3*x)")] + [solver_msg()] + verifier_msgs())
    records = asyncio.run(run_eval(MockClient(script), BANK, concurrency=1,
                                   with_diagnosis=True))
    wrong = records[1]
    assert wrong.outcome == "verdict" and wrong.verdict == "INCORRECT"
    assert wrong.diagnosis_error != ""
    m = compute_metrics(records)
    assert m.n_diagnosed == 0                          # errored -> excluded
    assert m.catch_rate == 1.0                         # verdict unaffected


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
