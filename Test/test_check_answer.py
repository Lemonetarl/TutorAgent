"""
test_check_answer.py -- run:  python -m pytest Test/test_check_answer.py -v
All four outcome paths of the single-submission checker, offline via
MockClient. capsys asserts on what the USER actually sees, since the printed
output IS this program's product.
"""
import asyncio

import pytest

from check_answer import check
from scripts.solver_pipeline import FakeMessage, MockClient, ToolUseBlock

STATEMENT = "Solve dy/dx = -3*y with y(0) = 1."


def parse_msg(answer_text, sympy_form) -> FakeMessage:
    student = None
    if answer_text is not None:
        student = {"answer_text": answer_text,
                   "values": [{"sympy_form": sympy_form}],
                   "units": None, "shows_work": False}
    return FakeMessage(content=[ToolUseBlock(id="tp", name="emit_parse", input={
        "reasoning": "x",
        "problem": {"subject_domain": "math", "statement": STATEMENT,
                    "is_complete": True, "has_multiple_parts": False},
        "target_quantity": "the solution y(x)",
        "student_answer": student,
        "ambiguity_flags": [], "ambiguity_notes": "", "confidence": "high"})])


def solver_msg(value="exp(-3*x)") -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="ts", name="emit_solution", input={
        "given": ["dy/dx = -3y", "y(0) = 1"], "unknowns": ["y"],
        "method_name": "separation_of_variables",
        "steps": [{"id": 1, "action": "solve", "equation": value,
                   "result": value}],
        "final_answer": {"value": value, "unit": None, "symbolic_form": value,
                         "answer_type": "symbolic", "additional_values": []}})])


def verifier_msgs(value="exp(-3*x)") -> list[FakeMessage]:
    return [FakeMessage(content=[ToolUseBlock(id="v1",
                name="check_symbolic_equivalence",
                input={"expr_a": value, "expr_b": value})]),
            FakeMessage(content=[ToolUseBlock(id="v2", name="submit_solution",
                input=solver_msg(value).content[0].input)])]


def diagnose_msg() -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="td", name="emit_diagnosis", input={
        "hypotheses": [
            {"misconception": "sign error in the exponent",
             "explanation": "writing +3x instead of -3x while integrating"}]})])


def adjudicator_msg() -> FakeMessage:
    return FakeMessage(content=[ToolUseBlock(id="ta", name="emit_adjudication",
        input={"first_divergent_solver_step": 1,
               "first_divergent_verifier_step": 1,
               "contradiction": "A says exp(-3x), B says exp(-4x)",
               "suspected_faulty": "unclear"})])


def run(script, raw):
    return asyncio.run(check(MockClient(script), raw))


def test_wrong_answer_gets_verdict_and_hypotheses(capsys):
    script = [parse_msg("exp(3*x)", "exp(3*x)"), solver_msg()] + \
             verifier_msgs() + [diagnose_msg()]
    s = run(script, f"Problem: {STATEMENT}\nMy answer: exp(3*x)")
    out = capsys.readouterr().out
    assert s["verdict"] == "INCORRECT" and s["diagnosed"]
    assert "VERDICT: INCORRECT" in out
    assert "ranked hypotheses" in out and "not proven" in out
    assert "sign error in the exponent" in out
    assert "verified answer : exp(-3*x)" in out


def test_correct_answer(capsys):
    script = [parse_msg("exp(-3*x)", "exp(-3*x)"), solver_msg()] + verifier_msgs()
    s = run(script, f"Problem: {STATEMENT}\nMy answer: exp(-3*x)")
    out = capsys.readouterr().out
    assert s["verdict"] in ("CORRECT", "EQUIVALENT")
    assert "VERDICT" in out and "INCORRECT" not in out


def test_no_answer_is_tutoring_mode_not_a_grade(capsys):
    script = [parse_msg(None, None), solver_msg()] + verifier_msgs()
    s = run(script, f"Problem: {STATEMENT}")
    out = capsys.readouterr().out
    assert s["stage"] == "solution_only" and s["verdict"] is None
    assert "nothing to grade" in out
    assert "verified solution" in out


def test_uncertain_refuses_a_verdict(capsys):
    # solver and verifier disagree for all 3 rounds -> blocked
    one_round = ([solver_msg("exp(-3*x)")] + verifier_msgs("exp(-4*x)")
                 + [adjudicator_msg()])
    script = [parse_msg("exp(3*x)", "exp(3*x)")] + one_round * 3
    s = run(script, f"Problem: {STATEMENT}\nMy answer: exp(3*x)")
    out = capsys.readouterr().out
    assert s["stage"] == "uncertain" and s["verdict"] is None
    assert "NO VERDICT" in out
    assert "human review" in out
    assert "INCORRECT" not in out       # never bluffs a verdict it doesn't have


def test_diagnosis_crash_keeps_the_verdict(capsys):
    # script ends before the diagnose message: S5 explodes, verdict survives
    script = [parse_msg("exp(3*x)", "exp(3*x)"), solver_msg()] + verifier_msgs()
    s = run(script, f"Problem: {STATEMENT}\nMy answer: exp(3*x)")
    out = capsys.readouterr().out
    assert s["verdict"] == "INCORRECT" and not s["diagnosed"]
    assert "couldn't be identified" in out
    assert "verdict above still stands" in out


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
