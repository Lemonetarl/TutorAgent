### An Independent AI Physics/Mathematics Tutor with Blind Solving, Answer Verification, and Misconception Diagnosis

## Overview

TutorAgent is an LLM-powered tutoring system designed to evaluate student solutions **without being influenced by the student's submitted answer**. 

Instead of comparing against an answer key, the system independently solves the problem, verifies its own reasoning, and then compares its verified solution against the student's work using deterministic symbolic and numerical tools.

The tutor is desigend with the idea that it should reason about the problem **independently** from the student's answer.

To accomplish this, the pipeline store the student's response from the solving process and introduces multiple validation stages before a verdict is produced.

---

# Motivation

Most existing AI tutors evaluate answers directly against the student's submission or rely entirely on an LLM's first response. 

This makes them vulnerable to: anchoring bias, hallucinated grading, and overconfident on incorrect feedback from the student.

Additionally, when asking LLM modles on homework help usually it will just gave answer and solution immediately, taking away the efforts student have to put in when solving problems set, removing the oppertutnity for the student to learn and understand the problem.

TutorAgent instead follows a multi-stage architecture inspired by verification pipelines used in modern AI reasoning systems.

---

# System Architecture

```
                Student Input
         (Problem + Student Answer)
                     │
                     ▼
            ┌──────────────────┐
            │     S1 Parser     │
            │ Extract & Separate│
            └──────────────────┘
                     │
     ┌───────────────┴────────────────┐
     │                                │
Problem Only                  Student Answer
     │                                │
     ▼                                │
┌──────────────────┐                  │
│ Independent Solver│                 │
│      (S2)         │                 │
└──────────────────┘                  │
     │                                │
     ▼                                │
┌──────────────────┐                  │
│ Blind Verifier   │                  │
│      (S3)        │                  │
└──────────────────┘                  │
     │                                │
     ▼                                │
 Verified Solution                     │
     │                                │
     └──────────────┬─────────────────┘
                    ▼
          ┌──────────────────┐
          │   Comparator (S4)│
          └──────────────────┘
                    │
                    ▼
          ┌──────────────────┐
          │ Diagnosis (S5)   │
          └──────────────────┘
                    │
                    ▼
          Feedback / Evaluation
```

---

# Core Design Principles

### 1. Blind Solving

The solver never receives the student's answer.

Instead, it receives only the problem statement and independently derives its own solution.

Perventing anchoring, confirmation bias, as well as copying incorrect reasoning from the student.
---

### 2. Independent Verification

The verifier **never** inspect the solver's reasoning.

Instead, it attempts to solve the problem independently and compares the resulting answer.

This reduces correlated reasoning failures.

---

### 3. Deterministic Answer Comparison

Rather than asking an LLM whether two answers are "close enough", TutorAgent performs symbolic and numerical comparison using

- SymPy
- Pint
- tolerance policies
- canonical forms

To ensure mathematically equivalent expressions are correctly recognized.

Examples:

```
1/2
0.5
5×10⁻¹
sqrt(8)/2
2*sqrt(2)/2
```

can all be evaluated appropriately.

---

### 4. Honest Uncertainty

If independent verification cannot confidently determine correctness, the system will return **UNCERTAIN** instead of fabricating feedback.

---

## Results

Measured across the 21-problem bank (24 trials: 8 correct answers,
16 injected-error answers), Sonnet 5 solver at k=1:

| Metric | Value | What it means |
|---|---|---|
| Catch rate | 100% (n=63) | of wrong answers that got a verdict, fraction flagged INCORRECT |
| False-positive rate | 11% (n=63) | of correct answers, fraction wrongly flagged |
| Uncertain rate | 2% | fraction where the system declined to issue a verdict rather than guess |
| Diagnosis top-1 | 50% (n=16) | of the three diagnosis the first diagnosis of error being correct |
| Diagnosis any | 81% (n=16) | of all three diagnosis the percentage of correct diagnosis obtained |

Reports are generated automatically into `runs/`; see
[`docs/eval_report_example.md`](docs/eval_report_example.md) for a full run.

## Scope & Limitations

**What it handles well:**
- Single-answer physics (Newtonian kinematics, energy) and math
  (algebra, first/second-order ODEs, single-variable calculus)
- Scalar answers with units, symbolic expressions, and root sets
- Answers in varied but equivalent forms (1/2, 0.5, sqrt(8)/2)

**What it does not handle (by design or by current limit):**
- Vector / multi-component answers — the comparator returns UNSUPPORTED
  rather than guessing; the bank loader skips these (7 of 21 problems)
- Multi-part problems (a), (b) — routed to clarification and seperation of problems, not graded
- Proofs and open-ended derivations — no single value to compare
- Problems requiring a figure or diagram
- Answers the parser can't extract cleanly — routed to "please clarify"
  rather than a guessed parse

**Known soft spots:**
- Verdicts at the 1% tolerance boundary (3 vs 3.03 s) are policy calls
- Diagnosis is a ranked hypothesis without mechanical replay (v2 work)
- Diagnosis matching is judged semantically by a small model (sonnet-5)

# Project Structure

```
TutorAgent/

scripts/
│
├── adapter.py
├── bank_loader.py
├── comparator.py
├── diagnose.py
├── diagnosis_judge.py
├── eval_harness.py
├── metrics.py
├── models.py
├── parser.py
├── prompts.py
├── report.py
├── solver_pipeline.py
└── validators.py

Test/
│
├── test_parser.py
├── test_comparator.py
├── test_solver_pipeline.py
├── test_eval_harness.py
├── test_diagnosis.py
├── test_check_answer.py 
└── ...

ProblemBank.json
requirements.txt
.env
```

---

# Pipeline Components

## S1 Parser

- separating problem and student answer
- validating extracted content
- checking completeness
- preventing information leakage
- producing structured Pydantic models

---

## S2 Independent Solver

Only recieve problem statment without the access to the student's work and generates

- symbolic solution
- numerical solution
- intermediate reasoning
- final answer
---

## S3 Blind Verification

Performs an independent solution attempt and validates

- symbolic correctness
- numerical correctness
- dimensional consistency
- consistency with governing equations

---

## S4 Comparator

Uses deterministic tools to compare

- symbolic expressions
- floating-point values
- units
- tolerances

Outputs one of

- Correct
- Equivalent
- Incorrect
- Uncertain

---

## S5 Diagnosis

Attempts to infer the student's misconception by comparing

- verified solution
- student answer
- common misconception taxonomy

Possible diagnoses include

- sign errors
- incorrect formulas
- algebra mistakes
- unit mistakes
- conceptual misunderstandings

---

# Evaluation Harness

The project includes an automated evaluation framework for measuring system performance.

Current metrics include

- Parsing accuracy
- Comparison accuracy
- Diagnosis accuracy
- Catch rate
- False positive rate
- Pipeline success rate

The evaluation harness can execute multiple problems from a JSON problem bank and generate summary reports.

---

# Technologies Used

- Python 3.12+
- Anthropic Claude API
- Pydantic
- SymPy
- Pint
- pytest
- JSON

---

# Repository Goals

This repository was built to investigate whether an LLM tutor can improve reliability through architectural constraints rather than its own line of reasoning.

The project focuses on

- reducing answer leakage
- deterministic mathematical validation
- blind verification
- uncertainty-aware reasoning
- misconception diagnosis
- automated evaluation

rather than maximizing benchmark scores.

---

# Running the Project

Install dependencies

```bash
pip install -r requirements.txt
```

Create an environment file in root folder

```text
ANTHROPIC_API_KEY=your_key_here
```

Check if everything is working

```bash
python -m pytest Test/ -v
```

Most tests run fully offline (the LLM is mocked). The tests that make real
API calls will **skip** automatically unless `ANTHROPIC_API_KEY` is set in
your `.env` — this is expected, not a failure.

Run unit tests

```bash
pytest -m scripts.check_answer
```
