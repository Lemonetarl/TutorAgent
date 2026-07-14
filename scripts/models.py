from typing import Optional, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

AmbiguityFlag = Literal[
    "units_unclear",
    "values_unclear",
    "problem_answer_boundary_unclear",
    "notation_ambiguous",
    "input_garbled",
    "subject_domain_unclear",
    "multiple_parts_unmapped",
    "problem_incomplete",
]

AnswerType = Literal["scalar","symbolic", "root_set", "other"]

_NULLISH = {"", "none", "null", "n/a", "na", "dimensionless", "-", "unknown"}

class AnswerValue(BaseModel):
    """One final value from the student. Separate the variable from the expression."""
    model_config = ConfigDict(extra="forbid")

    variable: Optional[str] = Field(
        default=None,
        description="The symbol being solved for, e.g. 'x', 'v_f'. Null for a bare quantity with no binding.",
    )
    sympy_form: str = Field(
        ...,
        description=(
            "The value as a SymPy-parseable expression, with NO '=' sign. "
            "Use ** for powers, * for multiplication, sqrt()/sin()/cos(), and Rational-friendly "
            "fractions. Examples: '2', '1/2', 'sqrt(2)*g*t', '2*x*cos(x**2)'. "
            "This is the ONLY field the comparator reads. Never emit LaTeX here."
        ),
    )
    latex: Optional[str] = Field(
        default=None,
        description="Optional LaTeX for display only, no $ delimiters. Never used for comparison.",
    )

    @field_validator("sympy_form")
    @classmethod
    def _no_equals(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sympy_form must not be empty")
        if "=" in v:
            raise ValueError(
                f"sympy_form must be the right-hand side only, got {v!r}. "
                "Put the symbol in `variable`."
            )
        return v

class StudentAnswer(BaseModel):
    """The student's answer, as literally written. Do not invent or correct any part of it."""
    model_config = ConfigDict(extra="forbid")

    answer_text: str = Field(                    
        ...,
        description="The student's response, copied character-for-character from the submission. This is a SUBSTRING of the submission, not the whole thing: it must NOT contain the problem statement."
    )

    values: list[AnswerValue] = Field(
        default_factory=list,
        description=(
            "Final answer value(s). One element for a single answer; several for multiple roots "
            "or multi-part answers. Empty list if the student wrote prose with no extractable value."
        ),    )    

    units: Optional[str] = Field(
        default=None,
        description="Units the student wrote, e.g. 'm/s'. Null if dimensionless or the student gave none. Do NOT supply units they omitted."
    )
    shows_work: bool = Field(
        default=False,
        description="True if the student showed intermediate steps, not just a final answer. Enables step-level diagnosis.",
    )

    @field_validator("answer_text")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "StudentAnswer with empty answer_text -- emit student_answer=null instead"
            )
        return v

    @field_validator("units")
    @classmethod
    def _normalise_units(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return None if v.strip().lower() in _NULLISH else v.strip()

    # Validator of whether or not the answer_text is empty. If it is, we raise an error to indicate that the student_answer should be null instead.
def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "StudentAnswer with empty answer_text -- emit student_answer=null instead"
            )
        return v

    #Function to normalize units, returning None if the units are nullish or empty, otherwise returning the stripped value.
def _normalize_units(cls, v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    if not v or v.lower() in _NULLISH:
        return None
    return v

class ReferenceAnswer(BaseModel):
    """The pipeline's OWN verified answer -- produced by the solver/verifier
    (S2/S3), never by the parser. to_reference() in solver_pipeline.py builds
    this from the winning FinalAnswer once the gate reports convergence.
    answer_type is a DESCRIPTION of an answer that already exists, not a
    prediction about a problem: that is why it lives here, not on ProblemOnly."""
    model_config = ConfigDict(extra="forbid")
 
    answer_type: AnswerType = Field(
        ...,
        description=(
            "The shape of the answer YOU just produced. scalar: a single number. "
            "symbolic: an expression in symbols. root_set: several discrete "
            "solutions. other: anything else -- routed to human review, never guessed."
        ),
    )
    values: list[AnswerValue] = Field(
        ...,
        min_length=1,
        description="The verified final value(s). Same format rules as AnswerValue everywhere.",
    )
    units: Optional[str] = Field(
        default=None,
        description="Units of the answer, e.g. 'm/s'. Null if dimensionless.",
    )
 
    @field_validator("units")
    @classmethod
    def _normalise_units(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return None if v.strip().lower() in _NULLISH else v.strip()
 
    @model_validator(mode="after")
    def _cardinality_matches_type(self) -> "ReferenceAnswer":
        n = len(self.values)
        if self.answer_type in ("scalar", "symbolic") and n != 1:
            raise ValueError(f"answer_type {self.answer_type!r} requires exactly one value, got {n}")
        if self.answer_type == "root_set" and n < 2:
            raise ValueError("root_set requires >= 2 values -- a single solution is answer_type 'scalar'")
        return self

class ProblemOnly(BaseModel):
    """A solvable problem, fully self-contained. Contains NO information from any student's answer."""
    model_config = ConfigDict(extra="forbid")

    subject_domain: Literal["math", "physics", "other"]

    statement: str = Field(
        ...,
        description="The problem, copied verbatim from the submission. Rewrite only where necessary to make it self-contained (e.g. pulling in a value stated elsewhere). Add nothing that is not in the source. Must contain NO information from the student's answer or work."
    )
    
    is_complete: bool = Field(
        ...,
        description="False if truncated, missing required information, or referencing an absent figure. Do not guess missing content."
    )

    has_multiple_parts: bool = Field(
        ...,
        description="True if the problem has sub-questions, e.g. (a), (b).",
    )


class ParsedInput(BaseModel):
    """Splits a student's raw submission into the problem and their answer, so the problem can be solved
    independently and the answer graded against that solution. Extract only what is literally present:
    never solve the problem, and never invent or correct any part of the student's answer."""
    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        ...,
        description="Analyze before filling any other field: where the problem ends and the answer begins, and anything ambiguous."
    )

    problem: Optional[ProblemOnly] = Field(
        None,
        description="Null if the submission contains no problem statement at all (e.g. the student pasted only an answer). Never invent a problem."
    )

    target_quantity: str = Field(..., description="What the problem asks for, in your own words. Empty string if there is no problem.")


    student_answer: Optional[StudentAnswer] = Field(
        None,
        description="Null if the input contains a problem with no student response."
    )

    
    ambiguity_flags: list[AmbiguityFlag] = Field(default_factory=list)
    
    ambiguity_notes: str = Field(default="", description="Detail on anything flagged. Empty if nothing is flagged.")
    
    confidence: Literal["high", "medium", "low"] = Field(
        ...,
        description="Quality of the problem/answer split. high: boundary unambiguous, every field directly supported by the text. medium: some inference needed but likely correct. low: garbled input, guessed boundary, or missing content. Prefer low over guessing."
    )

    def _downgrade(self, level: str) -> None:
        """Downgrade confidence to the given level if it is currently higher."""
        order = {"low": 0, "medium": 1, "high": 2}
        if order[level] < order[self.confidence]:
            self.confidence = level # type: ignore[assignment]

    @model_validator(mode="after")
    def _enforce_invariants(self) -> "ParsedInput":
        flags = list(dict.fromkeys(self.ambiguity_flags))  # dedupe, preserve order
        notes: list[str] = [self.ambiguity_notes] if self.ambiguity_notes else []
    
        if self.problem is None and self.student_answer is None:
            raise ValueError(
                    "Submission contains neither a problem nor an answer -- route to clarification, "
                    "do not enter the pipeline."
                )
        
        
        # A multi-part problem cannot be solved by the pipeline, because we cannot map the student's answer to the correct part. We downgrade to low confidence and flag it for clarification.
        if self.problem is not None and self.problem.has_multiple_parts:
            flags.append("multiple_parts_unmapped")
            notes.append("multi-part problem: answers cannot be mapped to parts -- ask the student to split")
            self._downgrade("low")         

        # Out of scope.
        if self.problem is not None and self.problem.subject_domain == "other":
            flags.append("subject_domain_unclear")
            notes.append("problem subject domain is 'other' -- cannot be solved by the pipeline")
            self._downgrade("low")   

        # Ill_posed
        if self.problem is not None and not self.problem.is_complete:
            flags.append("problem_incomplete")
            notes.append("problem is incomplete or references missing content -- cannot be solved by the pipeline")
            self._downgrade("low")
        
        # An answer with no extractable value cannot be comapred.
        if self.student_answer is not None and not self.student_answer.values:
            flags.append("values_unclear")
            notes.append("student answer has no extractable value -- cannot be compared to a solution")
            self._downgrade("low")
        
        # Flags and notes must travel together
        if flags and not notes:
            raise ValueError("ambiguity_flags set but ambiguity_notes empty -- explain the flags")
        if notes and not flags:
            raise ValueError("ambiguity_notes set but ambiguity_flags empty -- cannot explain anything")
        
        object.__setattr__(self, "ambiguity_flags", list(dict.fromkeys(flags)))  # dedupe, preserve order
        object.__setattr__(self, "ambiguity_notes", "|".join(n for n in notes if n))  # dedupe, preserve order, skip empty
        return self


    def ready_for_pipeline(self) -> bool:
            """True only if this can safely be solved and graded without asking the student anything."""
            return (
                self.confidence in ("high", "medium")
                and self.problem is not None
                and self.problem.is_complete
                and not self.problem.has_multiple_parts
                and self.problem.subject_domain in ("math", "physics")
            )
    
    def has_gradeable_answer(self) -> bool:
        """False -> tutoring mode, not verdict mode."""
        return self.student_answer is not None and bool(self.student_answer.values)
