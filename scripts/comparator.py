"""
comparator.py -- Stage 4 of the answer-checking pipeline.

THE CONTRACT
    compare(student_values, student_units, ref_values, ref_units,
            answer_type, domain) -> ComparisonResult

    verdict is one of:
        CORRECT      -- same answer, same form (string-identical after normalization)
        EQUIVALENT   -- same answer, different form (0.5 vs 1/2, km/h vs m/s,
                        sin(2x) vs 2sin(x)cos(x))
        INCORRECT    -- provably a different answer
        UNSUPPORTED  -- this comparator cannot judge it (unknown type, unparseable
                        input, undetermined equivalence). Routes to UNCERTAIN
                        upstream. NEVER guess.

DESIGN RULES (the interview answers live here):
  1. Pure Python. No API calls. A verdict must be deterministic and replayable.
  2. UNSUPPORTED is a verdict, not an exception. Honesty > coverage.
  3. Symbolic equivalence uses a ladder:
       structural -> numeric sampling -> simplify (in a killable subprocess).
     Sampling can PROVE inequality (one differing point suffices).
     simplify can PROVE equality (reduction to zero suffices).
     Neither can do the other's job reliably; use each for its strength.
  4. simplify() is not guaranteed to terminate fast, so it runs in a separate
     process with a hard timeout. signal.alarm is Unix-only; a subprocess
     works on Windows too.
"""
from __future__ import annotations

import multiprocessing as mp
import random
import re
from dataclasses import dataclass, field
from typing import Literal, Optional, cast

import sympy
from sympy import Number, simplify, sympify

try:
    import pint
    _UREG = pint.UnitRegistry()
    _DimensionalityError = pint.DimensionalityError
except ImportError:  # units become a no-op with a loud issue flag
    _UREG = None

Verdict = Literal["CORRECT", "EQUIVALENT", "INCORRECT", "UNSUPPORTED"]

_SAFE_EXPR = re.compile(r"^[0-9a-zA-Z_+\-*/^(). ,]*$")


def _sanitize(s: str) -> Optional[str]:
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or "__" in s or not _SAFE_EXPR.match(s):
        return None
    return s

# Physics tolerates rounding (default 1% relative); pure math is exact.
DOMAIN_REL_TOL = {"physics": 0.01, "math": 0.0}
SIMPLIFY_TIMEOUT_S = 5.0
SAMPLE_POINTS = 6
SAMPLE_TOL = 1e-9


@dataclass
class ComparisonResult:
    verdict: Verdict
    method: str                       # which rung of the ladder decided it
    detail: str = ""
    issues: list[str] = field(default_factory=list)

    @property
    def is_match(self) -> bool:
        return self.verdict in ("CORRECT", "EQUIVALENT")


# --------------------------------------------------------------------------
# Timeout machinery. Top-level functions only: Windows uses the 'spawn' start
# method, which pickles the target -- closures and lambdas won't survive it.
# --------------------------------------------------------------------------

def _task_simplify_zero(a_str: str, b_str: str) -> bool:
    return bool(simplify(sympify(a_str) - sympify(b_str)) == 0)


def _mp_runner(q: mp.Queue, args: tuple) -> None:
    try:
        q.put(("ok", _task_simplify_zero(*args)))
    except Exception as e:  # noqa: BLE001 -- any failure is just "undetermined"
        q.put(("err", repr(e)))


def _simplify_zero_with_timeout(a_str: str, b_str: str,
                                seconds: float = SIMPLIFY_TIMEOUT_S):
    """Returns True / False / None (None = timed out or errored)."""
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    p = ctx.Process(target=_mp_runner, args=(q, (a_str, b_str)), daemon=True)
    p.start()
    p.join(seconds)
    if p.is_alive():
        p.terminate()
        p.join(1.0)
        return None
    if not q.empty():
        status, value = q.get()
        return value if status == "ok" else None
    return None


# --------------------------------------------------------------------------
# The equivalence ladder (shared by symbolic, scalar and root-set paths,
# and by self-consistency majority voting in S2).
# --------------------------------------------------------------------------

def _rel_close(a: complex, b: complex, tol: float) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _numeric_sample(a: sympy.Expr, b: sympy.Expr,
                    tol: float = SAMPLE_TOL) -> Optional[bool]:
    """Substitute random values for all free symbols and compare.
    False  -> provably NOT equivalent (they differ at a real point).
    True   -> agreed at every sampled point (strong evidence, not proof).
    None   -> could not evaluate (singularities everywhere, etc.).
    Seeded RNG: same inputs must give the same verdict, forever."""
    syms = sorted(a.free_symbols | b.free_symbols, key=str)
    rng = random.Random(20260712)
    agreements = 0
    for _ in range(SAMPLE_POINTS):
        subs = {s: rng.uniform(0.7, 1.9) for s in syms}
        try:
            fa = complex(cast(Number, a.evalf(subs=subs)))
            fb = complex(cast(Number, b.evalf(subs=subs)))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if not _rel_close(fa, fb, tol):
            return False
        agreements += 1
    if agreements >= 3:
        return True
    return None


def expressions_equivalent(a_str: str, b_str: str) -> ComparisonResult:
    """The ladder. Also the 'same answer?' primitive for majority voting."""

    a_str, b_str = _sanitize(a_str), _sanitize(b_str) #type: ignore[assignment]

    if a_str is None or b_str is None:
        return ComparisonResult("UNSUPPORTED", "parse_error",
                                "expression failed the character whitelist",
                                ["parse_error"])
    try:
        a, b = sympify(a_str), sympify(b_str)
    except (sympy.SympifyError, SyntaxError, TypeError) as e:
        return ComparisonResult("UNSUPPORTED", "parse_error",
                                f"could not parse: {e}", ["parse_error"])
    
    if not (isinstance(a, sympy.Expr) and isinstance(b, sympy.Expr)):
        return ComparisonResult("UNSUPPORTED", "parse_error",
                                "sympify did not yield an expression "
                                "(non-string input reaching the comparator?)",
                                ["parse_error"])

    # Rung 1: structural. sympify canonicalizes commutative order, so
    # 'g*t*sqrt(2)' and 'sqrt(2)*g*t' are already identical objects.
    if a == b:
        return ComparisonResult("EQUIVALENT", "structural")

    # Rung 2: numeric sampling. One differing point is a PROOF of difference.
    sampled = _numeric_sample(a, b)
    if sampled is False:
        return ComparisonResult("INCORRECT", "numeric_sampling",
                                "expressions differ at a sampled point")

    # Rung 3: simplify, in a killable subprocess, to upgrade evidence to proof.
    reduced = _simplify_zero_with_timeout(a_str, b_str)
    if reduced is True:
        return ComparisonResult("EQUIVALENT", "symbolic_simplify")
    if sampled is True:
        # simplify failed or wasn't clever enough, but sampling agreed
        # everywhere. simplify(a-b) != 0 does NOT prove inequality.
        return ComparisonResult("EQUIVALENT", "numeric_sampling",
                                "simplify inconclusive; agreed at all sampled points")
    return ComparisonResult("UNSUPPORTED", "undetermined",
                            "neither sampling nor simplify could decide",
                            ["equivalence_undetermined"])


# --------------------------------------------------------------------------
# Scalar path: magnitude + units + tolerance.
# --------------------------------------------------------------------------

def _to_float(expr_str: str) -> Optional[float]:
    clean = _sanitize(expr_str)
    if clean is None:
        return None
    try:
        v = sympify(clean)
        if not isinstance(v, sympy.Expr):
            return None
        if v.free_symbols:
            return None
        result = v.evalf()
        if not isinstance(result, sympy.Float):   # <-- narrows the union to Float
            return None                            #     and catches zoo/oo/nan
        return float(result)
    except (sympy.SympifyError, SyntaxError, TypeError, ValueError):
        return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def compare_scalar(student_val: str, student_units: Optional[str],
                   ref_val: str, ref_units: Optional[str],
                   rel_tol: float) -> ComparisonResult:
    issues: list[str] = []
    
    if rel_tol == 0.0:
        # Exact domain (math): '1/3' vs '0.3333' must NOT pass. Delegate to
        # the symbolic ladder, which compares exactly.
        res = expressions_equivalent(student_val, ref_val)
        if res.verdict == "EQUIVALENT" and _norm(student_val) == _norm(ref_val):
            res.verdict = "CORRECT"
        return res

    s = _to_float(student_val)
    r = _to_float(ref_val)
    if s is None or r is None:
        return ComparisonResult("UNSUPPORTED", "parse_error",
                                "scalar value did not evaluate to a number",
                                ["parse_error"])

    converted = False
    if student_units and ref_units and _UREG is not None:
        try:
            qs = s * _UREG(student_units)
            qr = r * _UREG(ref_units)
            s = qs.to(qr.units).magnitude
            r = qr.magnitude
            converted = _norm(student_units) != _norm(ref_units)
        except _DimensionalityError:
            return ComparisonResult(
                "INCORRECT", "unit_dimension_mismatch",
                f"'{student_units}' and '{ref_units}' measure different things")
        except Exception:  # unparseable unit string -> compare magnitudes, loudly
            issues.append("units_unparseable")
    elif ref_units and not student_units:
        issues.append("units_missing")       # right number, no units: flag it,
    elif student_units and not ref_units:    # let the feedback stage teach it
        issues.append("unexpected_units")

    if not _rel_close(complex(s), complex(r), rel_tol):
        return ComparisonResult("INCORRECT", "numeric_tolerance",
                                f"student {s:g} vs reference {r:g} "
                                f"(tolerance {rel_tol:.0%})", issues)

    same_form = (_norm(student_val) == _norm(ref_val)) and not converted
    if same_form and not issues:
        return ComparisonResult("CORRECT", "exact", "", issues)
    method = "unit_converted" if converted else "numeric_tolerance"
    return ComparisonResult("EQUIVALENT", method,
                            "matches within tolerance", issues)


# --------------------------------------------------------------------------
# Root-set path: membership, not order. Every reference root must be hit by
# exactly one student root; extras are as wrong as omissions.
# --------------------------------------------------------------------------

def compare_root_set(student_vals: list[str],
                     ref_vals: list[str]) -> ComparisonResult:
    unmatched_ref = list(ref_vals)
    unmatched_stu = list(student_vals)
    any_form_diff = False

    for sv in list(unmatched_stu):
        for rv in unmatched_ref:
            res = expressions_equivalent(sv, rv)
            if res.is_match:
                unmatched_ref.remove(rv)
                unmatched_stu.remove(sv)
                if _norm(sv) != _norm(rv):
                    any_form_diff = True
                break

    if unmatched_ref:
        return ComparisonResult(
            "INCORRECT", "root_set",
            f"missing solution(s): {', '.join(unmatched_ref)}")
    if unmatched_stu:
        return ComparisonResult(
            "INCORRECT", "root_set",
            f"extra solution(s) that do not solve the problem: "
            f"{', '.join(unmatched_stu)}")
    verdict = "EQUIVALENT" if any_form_diff else "CORRECT"
    return ComparisonResult(verdict, "root_set")


# --------------------------------------------------------------------------
# The dispatch table -- the single public entry point.
# --------------------------------------------------------------------------

def compare(student_values: list[str], student_units: Optional[str],
            ref_values: list[str], ref_units: Optional[str],
            answer_type: str, domain: str = "physics") -> ComparisonResult:
    if not student_values or not ref_values:
        return ComparisonResult("UNSUPPORTED", "empty_input",
                                "no value to compare", ["values_missing"])

    rel_tol = DOMAIN_REL_TOL.get(domain, 0.01)

    for v in (*student_values, *ref_values):
        if not isinstance(v, str):
            raise TypeError(
                f"compare() takes list[str], got {type(v).__name__}. "
                f"Pass [v.sympy_form for v in answer.values], not the AnswerValue "
                f"objects themselves -- or call adapter.grade(), which does it for you.")
 
    rel_tol = DOMAIN_REL_TOL.get(domain, 0.01)

    if answer_type == "scalar":
        if len(student_values) != 1 or len(ref_values) != 1:
            return ComparisonResult("UNSUPPORTED", "cardinality",
                                    "scalar expected exactly one value each",
                                    ["cardinality_mismatch"])
        return compare_scalar(student_values[0], student_units,
                              ref_values[0], ref_units, rel_tol)

    if answer_type == "symbolic":
        if len(student_values) != 1 or len(ref_values) != 1:
            return ComparisonResult("UNSUPPORTED", "cardinality",
                                    "symbolic expected exactly one value each",
                                    ["cardinality_mismatch"])
        res = expressions_equivalent(student_values[0], ref_values[0])
        if (res.verdict == "EQUIVALENT"
                and _norm(student_values[0]) == _norm(ref_values[0])):
            res.verdict = "CORRECT"
        return res

    if answer_type == "root_set":
        return compare_root_set(student_values, ref_values)

    # vector, interval, proof, other: honestly out of scope for v1.
    return ComparisonResult("UNSUPPORTED", "answer_type",
                            f"comparator has no handler for '{answer_type}'",
                            ["unsupported_answer_type"])


def answers_match(a: str, b: str) -> bool:
    """The primitive S2's majority vote calls. Deliberately strict:
    an undetermined comparison is NOT a match."""
    return expressions_equivalent(a, b).is_match

