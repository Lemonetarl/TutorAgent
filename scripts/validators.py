
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

import sympy
from sympy import sympify

from scripts.models import AmbiguityFlag, ParsedInput
# The character whitelist is defined once, in the comparator, and imported
# here so the two can never drift. (Yes, it is underscore-private; treating
# the comparator as this project's owner of "what is a safe expression" is
# the point.)
from scripts.comparator import _sanitize

Downgrade = Literal["medium", "low"]

# Reconstruction thresholds: fraction of the submission's tokens that must
# reappear in (statement + answer_text).
COVERAGE_LOW = 0.50      # below this -> confidence low
COVERAGE_MEDIUM = 0.75   # below this -> confidence medium
MIN_TOKENS_FOR_COVERAGE = 8   # tiny inputs make coverage meaningless

_ASK_WORDS = re.compile(
    r"\?|\b(find|solve|determine|calculate|compute|evaluate|derive|show|"
    r"how (?:long|far|fast|much|many)|what|when|where)\b", re.IGNORECASE)

_WORD = re.compile(r"[A-Za-z0-9.]+")


@dataclass(frozen=True)
class Finding:
    # AmbiguityFlag, not str: a typo like "leakage" is now a type error at the
    # point the Finding is CREATED, rather than an untyped string that only
    # explodes when ParsedInput.ambiguity_flags rejects it at runtime.
    flag: AmbiguityFlag
    note: str
    downgrade_to: Downgrade


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text)]


def _ws_norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


# ---------------------------------------------------------------------------
# The validators
# ---------------------------------------------------------------------------

def validate_leakage(raw_text: str, parsed: ParsedInput) -> list[Finding]:
    """I1's parse-time tripwire: a student value appearing inside the problem
    statement means the split failed to separate the two. Token-boundary
    matched so '2.0' never fires inside '2.02'. A hit is a WARNING (data can
    legitimately coincide with the answer); the hard guarantee stays with
    enforce_isolation() in solver_pipeline."""
    if parsed.problem is None or parsed.student_answer is None:
        return []
    haystack = f"{parsed.problem.statement} {parsed.target_quantity}"
    for av in parsed.student_answer.values:
        needle = av.sympy_form.strip()
        if len(needle) > 1 and re.search(
                rf"(?<![\w.]){re.escape(needle)}(?![\w.])", haystack):
            return [Finding(
                "problem_answer_boundary_unclear",
                f"possible leakage: student value {needle!r} appears in the problem",
                "medium")]
    return []


def validate_answer_verbatim(raw_text: str, parsed: ParsedInput) -> list[Finding]:
    """answer_text's contract is character-for-character. If it is not a
    (whitespace-normalized) substring of the submission, the model paraphrased
    or invented -- exactly the false-confidence failure S5 must never inherit."""
    if parsed.student_answer is None:
        return []
    if _ws_norm(parsed.student_answer.answer_text) not in _ws_norm(raw_text):
        return [Finding(
            "problem_answer_boundary_unclear",
            "answer_text is not a verbatim substring of the submission -- "
            "the extraction paraphrased or invented the student's answer",
            "low")]
    return []


def validate_reconstruction(raw_text: str, parsed: ParsedInput) -> list[Finding]:
    """Coverage check: (statement + answer_text) should account for most of
    the submission's tokens. Low coverage means content was silently dropped,
    which is worse than a visible failure because it corrupts everything
    downstream while looking clean."""
    raw_tokens = _tokens(raw_text)
    if len(raw_tokens) < MIN_TOKENS_FOR_COVERAGE:
        return []
    captured: set[str] = set()
    if parsed.problem is not None:
        captured.update(_tokens(parsed.problem.statement))
    if parsed.student_answer is not None:
        captured.update(_tokens(parsed.student_answer.answer_text))
    coverage = sum(1 for t in raw_tokens if t in captured) / len(raw_tokens)
    if coverage < COVERAGE_LOW:
        return [Finding("input_garbled",
                        f"only {coverage:.0%} of the submission's tokens were "
                        f"captured -- content was silently dropped", "low")]
    if coverage < COVERAGE_MEDIUM:
        return [Finding("input_garbled",
                        f"{coverage:.0%} token coverage -- some content may "
                        f"have been dropped", "medium")]
    return []


def validate_completeness(raw_text: str, parsed: ParsedInput) -> list[Finding]:
    """Second opinion on is_complete: a 'problem' that never asks anything, or
    an empty target_quantity, cannot be solved even if the model called it
    complete."""
    if parsed.problem is None or not parsed.problem.is_complete:
        return []          # model already routed it out; nothing to add
    findings: list[Finding] = []
    if not _ASK_WORDS.search(parsed.problem.statement):
        findings.append(Finding(
            "problem_incomplete",
            "the statement never asks for anything (no question mark or task "
            "verb found)", "medium"))
    if not parsed.target_quantity.strip():
        findings.append(Finding(
            "problem_incomplete",
            "a problem is present but target_quantity is empty", "medium"))
    return findings


def validate_values_parse(raw_text: str, parsed: ParsedInput) -> list[Finding]:
    """Every sympy_form must ACTUALLY parse. Catching 'exp(3x)' (implicit
    multiplication) here, at parse time, is what lets the error-feedback retry
    fix it for free instead of the comparator choking on it later. Values are
    short, so no subprocess guard is needed here."""
    if parsed.student_answer is None:
        return []
    findings: list[Finding] = []
    for av in parsed.student_answer.values:
        clean = _sanitize(av.sympy_form)
        ok = False
        if clean is not None:
            try:
                ok = isinstance(sympify(clean), sympy.Expr)
            except (sympy.SympifyError, SyntaxError, TypeError, ValueError):
                ok = False
        if not ok:
            findings.append(Finding(
                "values_unclear",
                f"value {av.sympy_form!r} is not SymPy-parseable "
                f"(remember explicit '*': 'exp(3*x)', never 'exp(3x)')",
                "low"))
    return findings


ALL_VALIDATORS: list[Callable[[str, ParsedInput], list[Finding]]] = [
    validate_leakage,
    validate_answer_verbatim,
    validate_reconstruction,
    validate_completeness,
    validate_values_parse,
]


def run_validators(raw_text: str, parsed: ParsedInput) -> list[Finding]:
    findings: list[Finding] = []
    for v in ALL_VALIDATORS:
        findings.extend(v(raw_text, parsed))
    return findings


# ---------------------------------------------------------------------------
# Applying findings. Mutation via object.__setattr__ (the same mechanism the
# model's own validator uses) rather than reconstruction, so
# _enforce_invariants does not re-run and duplicate its auto-notes.
# ---------------------------------------------------------------------------

_ORDER = {"low": 0, "medium": 1, "high": 2}


def apply_findings(parsed: ParsedInput, findings: list[Finding]) -> ParsedInput:
    if not findings:
        return parsed
    flags: list[AmbiguityFlag] = list(parsed.ambiguity_flags)
    notes: list[str] = [parsed.ambiguity_notes] if parsed.ambiguity_notes else []
    confidence: Literal["high", "medium", "low"] = parsed.confidence
    for f in findings:
        flags.append(f.flag)
        notes.append(f.note)
        if _ORDER[f.downgrade_to] < _ORDER[confidence]:
            confidence = f.downgrade_to
    object.__setattr__(parsed, "ambiguity_flags", list(dict.fromkeys(flags)))
    object.__setattr__(parsed, "ambiguity_notes",
                       " | ".join(dict.fromkeys(n for n in notes if n)))
    object.__setattr__(parsed, "confidence", confidence)
    return parsed