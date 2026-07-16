"""
test_comparator.py -- run with:  pytest test_comparator.py -v
Each test name states the case; each is also a golden regression case:
if a future change flips any verdict, CI should scream.
"""
import pytest
from scripts.comparator import compare, answers_match

P, M = "physics", "math"


# ---------------- scalar / physics (1% relative tolerance) ----------------

def test_exact_same_scalar_same_units():
    r = compare(["2.02"], "s", ["2.02"], "s", "scalar", P)
    assert r.verdict == "CORRECT"

def test_rounding_within_tolerance():
    # 2.0199 vs 2.02: same answer, differently rounded
    r = compare(["2.02"], "s", ["2.0199"], "s", "scalar", P)
    assert r.verdict == "EQUIVALENT" and r.is_match

def test_g_981_vs_98_within_1pct():
    r = compare(["9.81"], "m/s**2", ["9.8"], "m/s**2", "scalar", P)
    assert r.verdict == "EQUIVALENT"

def test_unit_conversion_kmh_to_ms():
    # 3.6 km/h == 1 m/s exactly
    r = compare(["3.6"], "km/h", ["1"], "m/s", "scalar", P)
    assert r.verdict == "EQUIVALENT" and r.method == "unit_converted"

def test_wrong_magnitude():
    r = compare(["2.5"], "m", ["2.0"], "m", "scalar", P)
    assert r.verdict == "INCORRECT"

def test_dimension_mismatch_meters_vs_seconds():
    r = compare(["5"], "m", ["5"], "s", "scalar", P)
    assert r.verdict == "INCORRECT" and r.method == "unit_dimension_mismatch"

def test_missing_units_right_magnitude_is_flagged_not_failed():
    r = compare(["5"], None, ["5"], "m/s", "scalar", P)
    assert r.is_match and "units_missing" in r.issues

def test_garbled_scalar_routes_to_unsupported_not_a_guess():
    r = compare(["2..02"], "s", ["2.02"], "s", "scalar", P)
    assert r.verdict == "UNSUPPORTED"


# ---------------- scalar / math (exact -- no tolerance) ----------------

def test_half_vs_decimal_half_equivalent():
    r = compare(["1/2"], None, ["0.5"], None, "scalar", M)
    assert r.verdict == "EQUIVALENT"

def test_identical_strings_are_correct_not_equivalent():
    r = compare(["0.5"], None, ["0.5"], None, "scalar", M)
    assert r.verdict == "CORRECT"

def test_rationalized_surd():
    r = compare(["sqrt(2)/2"], None, ["1/sqrt(2)"], None, "scalar", M)
    assert r.verdict == "EQUIVALENT"

def test_truncated_decimal_is_NOT_one_third_in_math():
    # the case that justifies exact mode: 0.3333 != 1/3
    r = compare(["0.3333"], None, ["1/3"], None, "scalar", M)
    assert r.verdict == "INCORRECT"


# ---------------- symbolic ----------------

def test_identical_expression():
    r = compare(["2*x*cos(x**2)"], None, ["2*x*cos(x**2)"], None, "symbolic", M)
    assert r.verdict == "CORRECT"

def test_double_angle_identity():
    r = compare(["sin(2*x)"], None, ["2*sin(x)*cos(x)"], None, "symbolic", M)
    assert r.verdict == "EQUIVALENT"

def test_factored_vs_expanded():
    r = compare(["(x-1)*(x+1)"], None, ["x**2 - 1"], None, "symbolic", M)
    assert r.verdict == "EQUIVALENT"

def test_sign_error_is_caught():
    r = compare(["x**2 + 1"], None, ["x**2 - 1"], None, "symbolic", M)
    assert r.verdict == "INCORRECT"

def test_commutative_reordering():
    r = compare(["g*t*sqrt(2)"], None, ["sqrt(2)*g*t"], None, "symbolic", P)
    assert r.is_match


# ---------------- root sets ----------------

def test_roots_order_free():
    r = compare(["1", "-1"], None, ["-1", "1"], None, "root_set", M)
    assert r.verdict == "CORRECT"

def test_missing_root_is_incorrect():
    r = compare(["1"], None, ["1", "-1"], None, "root_set", M)
    assert r.verdict == "INCORRECT" and "missing" in r.detail

def test_extra_root_is_incorrect():
    r = compare(["1", "-1", "0"], None, ["1", "-1"], None, "root_set", M)
    assert r.verdict == "INCORRECT" and "extra" in r.detail

def test_roots_in_different_forms():
    r = compare(["1/2", "-2"], None, ["0.5", "-2"], None, "root_set", M)
    assert r.verdict == "EQUIVALENT"


# ---------------- honest refusals ----------------

def test_vector_is_unsupported_not_guessed():
    r = compare(["(3, 4)"], None, ["(3, 4)"], None, "vector", P)
    assert r.verdict == "UNSUPPORTED"

def test_empty_student_values():
    r = compare([], None, ["5"], "m", "scalar", P)
    assert r.verdict == "UNSUPPORTED"


# ---------------- the majority-vote primitive (S2 depends on this) --------

def test_majority_vote_primitive_matches():
    assert answers_match("sqrt(2)*g*t", "g*t*2**(1/2)")

def test_majority_vote_primitive_rejects():
    assert not answers_match("g*t**2/2", "g*t**2")

def test_majority_vote_unparseable_is_not_a_match():
    assert not answers_match("2..02", "2.02")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))


# ---------------- the whitelist (sympify uses eval; students type here) ----

def test_injection_is_rejected_not_executed():
    from scripts.comparator import expressions_equivalent
    r = expressions_equivalent("__import__('os').getcwd()", "2.02")
    assert r.verdict == "UNSUPPORTED" and "whitelist" in r.detail

def test_whitelist_rejects_in_scalar_path_too():
    r = compare(["__import__('os').getcwd()"], "s", ["2.02"], "s", "scalar", P)
    assert r.verdict == "UNSUPPORTED"
