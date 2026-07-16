from typing import Optional

from scripts.models import ParsedInput, ReferenceAnswer
from scripts.comparator import ComparisonResult, compare

def grade(parsed: ParsedInput, reference: ReferenceAnswer) -> ComparisonResult:
    """Compare the student's parsed answer against the pipeline's
    verified reference answer. Refuses (UNSUPPORTED) rather than guesses
    whenever the input isn't in a gradeable state."""
    if not parsed.ready_for_pipeline():
        return ComparisonResult("UNSUPPORTED", "not_ready",
                                f"parse not pipeline-ready: {parsed.ambiguity_notes}",
                                list(parsed.ambiguity_flags))
    if not parsed.has_gradeable_answer():
        return ComparisonResult("UNSUPPORTED", "no_answer",
                                "no gradeable answer -- tutoring mode",
                                ["values_missing"])

    assert parsed.problem is not None and parsed.student_answer is not None
    return compare(
        student_values=[v.sympy_form for v in parsed.student_answer.values],
        student_units=parsed.student_answer.units,
        ref_values=[v.sympy_form for v in reference.values],
        ref_units=reference.units,
        answer_type=reference.answer_type,
        domain=parsed.problem.subject_domain,
    )