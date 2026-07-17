
from __future__ import annotations

import time
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from scripts.comparator import ComparisonResult
from scripts.models import ParsedInput, ReferenceAnswer

DIAGNOSIS_MODEL = "claude-sonnet-5"
DIAGNOSIS_PROMPT_VERSION = "v1"
MAX_TOKENS = 8000   # adaptive thinking counts against this


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    misconception: str = Field(
        ..., description="Short name of the specific error, e.g. 'dropped the "
                         "factor of 2 inside the square root'. Not a topic.")
    explanation: str = Field(
        ..., description="How exactly this error produces the student's exact "
                         "answer from this problem. Show the faulty derivation "
                         "briefly.")


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypotheses: list[Hypothesis] = Field(
        ..., min_length=1, max_length=3,
        description="Ranked, most plausible FIRST. 2-3 entries.")


DIAGNOSIS_SYSTEM = """\
You are an expert physics and mathematics tutor diagnosing WHY a student's
answer is wrong. You are given the problem, the student's exact answer, the
verified correct answer, and the numeric/symbolic comparison detail.

Produce 2-3 hypotheses, RANKED most plausible first, for the specific
mistake that generated the student's exact value.

RULES
1. Each hypothesis must mechanically explain the student's EXACT answer.
   Test it mentally: apply the hypothesized error to this problem -- do you
   get the student's value? If not, it is not a valid hypothesis.
2. Name the mechanism, not the topic. "Forgot the 1/2 in KE = (1/2)mv**2"
   is a diagnosis; "struggles with energy" is not.
3. Common mechanisms to consider (not exhaustive): sign error; dropped or
   doubled constant/factor; swapped two given values; used an adjacent
   formula (v=at vs d=at**2/2); missing chain rule; ignored an initial
   condition; unit conversion slip; incomplete solution (missing roots);
   arithmetic slip at a specific step.
4. Do not lecture, do not address the student, do not propose feedback.
   Diagnosis only.
5. Emit exactly one emit_diagnosis tool call.
"""


def build_diagnosis_user(parsed: ParsedInput, reference: ReferenceAnswer,
                         comparison: ComparisonResult) -> str:
    assert parsed.problem is not None and parsed.student_answer is not None
    sa = parsed.student_answer
    student_vals = ", ".join(v.sympy_form for v in sa.values) or "(none)"
    ref_vals = ", ".join(v.sympy_form for v in reference.values)
    return (
        f"PROBLEM:\n{parsed.problem.statement}\n\n"
        f"ASKED FOR: {parsed.target_quantity}\n\n"
        f"STUDENT'S ANSWER (verbatim): {sa.answer_text}\n"
        f"  extracted value(s): {student_vals}"
        f"{f'  units: {sa.units}' if sa.units else ''}\n\n"
        f"VERIFIED CORRECT ANSWER: {ref_vals}"
        f"{f'  units: {reference.units}' if reference.units else ''}\n\n"
        f"COMPARISON DETAIL: {comparison.detail or comparison.method}\n\n"
        f"Diagnose the specific error that produced the student's exact value.")


async def diagnose(client, *, parsed: ParsedInput, reference: ReferenceAnswer,
                   comparison: ComparisonResult) -> tuple[Diagnosis, int, int]:
    """Returns (diagnosis, input_tokens, output_tokens)."""
    user = build_diagnosis_user(parsed, reference, comparison)
    msg = await client.create(
        model=DIAGNOSIS_MODEL, max_tokens=MAX_TOKENS,
        system=DIAGNOSIS_SYSTEM,
        messages=[{"role": "user", "content": user}],
        tools=[{"name": "emit_diagnosis",
                "description": "Emit the ranked misconception hypotheses. Exactly once.",
                "input_schema": Diagnosis.model_json_schema()}],
        tool_choice={"type": "tool", "name": "emit_diagnosis"})
    tin = getattr(msg.usage, "input_tokens", 0)
    tout = getattr(msg.usage, "output_tokens", 0)
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_diagnosis":
            return Diagnosis.model_validate(block.input), tin, tout
    raise ValueError("diagnose: model did not emit emit_diagnosis")
