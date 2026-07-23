# Eval report -- 2026-07-17 21:26

Config: solver=`claude-sonnet-5` (k=1), parser=`claude-haiku-4-5`, concurrency=3, banks=['ProblemBank.json']

## Headline metrics

| metric | value | definition |
|---|---|---|
| **catch rate** | **100%** | of wrong answers that got a verdict, fraction flagged INCORRECT |
| **false-positive rate** | **0%** | of correct answers that got a verdict, fraction wrongly flagged |
| uncertain rate | 4% | of pipeline entries, fraction that BLOCKED (honest refusal, excluded from the rates above) |
| uncertain (correct / wrong) | 0% / 6% | uncertainty split by trial kind |
| **diagnosis top-1 accuracy** | **50%** | of caught wrong answers diagnosed (n=16), the TOP hypothesis matched the injected cause (semantic judge) |
| diagnosis any-hypothesis accuracy | 81% | any of the <=3 ranked hypotheses matched the injected cause |

## Outcome breakdown

- trials: 60 (20 correct-answer, 40 injected-wrong)
- verdicts: 45 | uncertain: 2 | not pipeline-ready: 0 | errors: 13

## Cost and latency

- tokens: 804501 in / 185178 out
- wall time: 26190.9s total, 436.5s mean per trial
- caveat: wall time overlaps under concurrency; per-trial seconds are honest, the total is not a sum of a serial run

## Exemplars: honest refusals (Invariant 3 on camera)

Trials where the pipeline declined to issue a verdict rather than guess. With k=1, these are the runs where the blind verifier -- now the load-bearing check -- disagreed with the solver to the end:

- `First_Order_ODE_2` (wrong, injected: Forgot to apply for the initial condition incorrectly and used C=1 instread of C=3.) -> trace `runs\trace_a7666a8c7e.json`
- `First_Order_ODE_2` (wrong, injected: Integrated 2 incorrectly, giving exp(x) instead of exp(2*x)) -> trace `runs\trace_56bc7935ed.json`

## Per-trial results

| problem | kind | outcome | verdict | injected cause | top diagnosis | tok in/out | s |
|---|---|---|---|---|---|---|---|
| First_Order_ODE_1 | correct | verdict | CORRECT |  |  | 12464/2411 | 28.6 |
| First_Order_ODE_1 | wrong | verdict | INCORRECT | Dropping sign during calculation | Sign error in separating variables / integrating (hit@1) | 30945/5426 | 64.2 |
| First_Order_ODE_1 | wrong | verdict | INCORRECT | Forgot to apply for the initial condition and left the integration constant as a arbitary constant | Sign error when separating variables/integrating (hit@2) | 21066/4102 | 50.8 |
| First_Order_ODE_2 | correct | error |  |  |  | 3431/388 | 64.6 |
| First_Order_ODE_2 | wrong | uncertain |  | Forgot to apply for the initial condition incorrectly and used C=1 instread of C=3. |  | 30292/6344 | 131.9 |
| First_Order_ODE_2 | wrong | uncertain |  | Integrated 2 incorrectly, giving exp(x) instead of exp(2*x) |  | 32738/6208 | 141.2 |
| First_Order_ODE_3 | correct | error |  |  |  | 3431/403 | 88.5 |
| First_Order_ODE_3 | wrong | verdict | INCORRECT | Forgot that ∫y^(-2)dy = -1/y, leading to the wrong sign. | Sign error when integrating 1/y^2 dy (hit@2) | 25128/5960 | 150.1 |
| First_Order_ODE_3 | wrong | verdict | INCORRECT | Solved for the integration constant incorrectly after applying y(0)=1. | Sign error in integrating 1/y^2 dy (hit@3) | 13336/3815 | 167.9 |
| Second_Order_ODE_1 | correct | error |  |  |  | 3495/395 | 175.6 |
| Second_Order_ODE_1 | wrong | error |  | Forgot that repeated roots require an additional x factor. |  | 3477/387 | 182.3 |
| Second_Order_ODE_1 | wrong | verdict | INCORRECT | Made a sign error while solving for the constants. | (diagnosis errored) | 10257/2973 | 215.1 |
| Second_Order_ODE_2 | correct | verdict | CORRECT |  |  | 9908/2267 | 199.2 |
| Second_Order_ODE_2 | wrong | verdict | INCORRECT | Used exponential solutions instead of trigonometric solutions for complex roots. | (diagnosis errored) | 9826/2354 | 221.3 |
| Second_Order_ODE_2 | wrong | verdict | INCORRECT | Applied the initial conditions incorrectly and swapped the coefficients. | (diagnosis errored) | 19848/5458 | 266.2 |
| Calculus_Differentiation_1 | correct | verdict | EQUIVALENT |  |  | 10241/3077 | 248.7 |
| Calculus_Differentiation_1 | wrong | verdict | INCORRECT | Forgot the sine component after solving for the constants. | Answer does not correspond to differentiating the given func (miss) | 13213/3865 | 266.8 |
| Calculus_Differentiation_1 | wrong | verdict | INCORRECT | Differentiated incorrectly when applying the initial conditions, giving an incorrect coefficient. | Answer belongs to a completely different problem (mismatched (miss) | 13096/3639 | 291.7 |
| Calculus_Integration_1 | correct | verdict | EQUIVALENT |  |  | 9863/2408 | 291.3 |
| Calculus_Integration_1 | wrong | error |  | Forgot to subtract the value of the antiderivative at the lower limit. |  | 3465/429 | 297.2 |
| Calculus_Integration_1 | wrong | verdict | INCORRECT | Made an algebra error during integration by parts. | Sign error in integration by parts reduction formula, kept a (hit@1) | 37270/9479 | 398.8 |
| Calculus_Optimization_1 | correct | verdict | CORRECT |  |  | 9634/1881 | 310.4 |
| Calculus_Optimization_1 | wrong | error |  | Reported the x-coordinate of the maximum instead of the maximum value. |  | 3453/368 | 321.6 |
| Calculus_Optimization_1 | wrong | verdict | INCORRECT | Made an arithmetic mistake when evaluating the function at x=3. | (diagnosis errored) | 9603/1779 | 346.0 |
| Kinematics_1st_1 | correct | verdict | CORRECT |  |  | 9834/2078 | 346.0 |
| Kinematics_1st_1 | wrong | verdict | INCORRECT | Forgot to divide by the drag coefficient and used v = mg. | Dropped mass factor in terminal velocity formula (used v=g/b (hit@1) | 12866/2762 | 378.7 |
| Kinematics_1st_1 | wrong | error |  | Used terminal velocity as g instead of solving mg = bv. |  | 3533/461 | 351.0 |
| Kinematics_1st_2 | correct | error |  |  |  | 3519/442 | 373.7 |
| Kinematics_1st_2 | wrong | verdict | INCORRECT | Forgot to convert dv/dt into v(dv/dx), leading to the time-domain solution. | (diagnosis errored) | 13766/3055 | 419.6 |
| Kinematics_1st_2 | wrong | error |  | Made a sign error while separating variables. |  | 3517/528 | 419.6 |
| Kinematics_2nd_1 | correct | error |  |  |  | 3519/474 | 427.8 |
| Kinematics_2nd_1 | wrong | verdict | INCORRECT | Applied the initial conditions incorrectly. | Used sine solution form without applying initial conditions (hit@1) | 26870/6792 | 491.1 |
| Kinematics_2nd_1 | wrong | verdict | INCORRECT | Forgot the square root when computing the natural frequency. | (diagnosis errored) | 27700/7022 | 493.2 |
| Kinematics_Basic_1 | correct | verdict | EQUIVALENT |  |  | 9646/1926 | 448.6 |
| Kinematics_Basic_1 | wrong | verdict | INCORRECT | Forgot the factor of 1/2 in the displacement equation and used h=gt². | Dropped the factor of 2 in the kinematics equation h = (1/2) (hit@1) | 12679/2524 | 476.8 |
| Kinematics_Basic_1 | wrong | verdict | INCORRECT | Forgot to take the square root after solving for t². | (diagnosis errored) | 12064/1785 | 507.7 |
| Kinematics_Basic_2 | correct | verdict | EQUIVALENT |  |  | 9567/1750 | 509.8 |
| Kinematics_Basic_2 | wrong | verdict | INCORRECT | Forgot the factor of 2 and used h=u²/g. | Dropped the factor of 2 in h = v0^2/(2g) (hit@1) | 12416/2211 | 519.1 |
| Kinematics_Basic_2 | wrong | verdict | INCORRECT | Incorrectly divided by 4g instead of 2g. | (diagnosis errored) | 9562/1947 | 530.2 |
| Kinematics_Basic_3 | correct | verdict | EQUIVALENT |  |  | 9779/2123 | 534.1 |
| Kinematics_Basic_3 | wrong | verdict | INCORRECT | Used the total launch speed instead of the vertical component. | Dropped the factor of 2 in the denominator of h = v0y^2/(2g) (hit@2) | 12688/2750 | 548.6 |
| Kinematics_Basic_3 | wrong | verdict | INCORRECT | Forgot the factor of 2 in the kinematic equation. | (diagnosis errored) | 9894/2178 | 557.5 |
| Kinematics_Basic_4 | correct | verdict | EQUIVALENT |  |  | 10021/2486 | 558.7 |
| Kinematics_Basic_4 | wrong | verdict | INCORRECT | Used t=√(h/g) instead of √(2h/g). | (diagnosis errored) | 10328/2502 | 580.8 |
| Kinematics_Basic_4 | wrong | error |  | Confused the platform height with the horizontal distance. |  | 7177/828 | 563.3 |
| Kinematics_Basic_5 | correct | verdict | EQUIVALENT |  |  | 17166/5260 | 613.5 |
| Kinematics_Basic_5 | wrong | verdict | INCORRECT | Used sin(theta) instead of tan(theta) when resolving the normal force. | (diagnosis errored) | 39521/13818 | 716.5 |
| Kinematics_Basic_5 | wrong | error |  | Resolved the force components incorrectly. |  | 3513/472 | 694.9 |
| Kinematics_Basic_6 | correct | verdict | EQUIVALENT |  |  | 10417/3129 | 643.9 |
| Kinematics_Basic_6 | wrong | verdict | INCORRECT | Added the tangential and centripetal accelerations directly instead of using the Pythagorean theorem. | Added tangential and centripetal acceleration linearly inste (hit@1) | 27386/5401 | 706.2 |
| Kinematics_Basic_6 | wrong | verdict | INCORRECT | Ignored the centripetal acceleration. | (diagnosis errored) | 21164/5842 | 756.1 |
| Kinematics_Basic_7 | correct | verdict | CORRECT |  |  | 13345/2935 | 735.1 |
| Kinematics_Basic_7 | wrong | verdict | INCORRECT | Forgot to apply the initial velocity after integrating acceleration. | Dropped the initial velocity term when integrating velocity  (hit@2) | 23026/5670 | 775.3 |
| Kinematics_Basic_7 | wrong | verdict | INCORRECT | Integrated 2t² incorrectly. | Skipped dividing by the new exponent when integrating the 4t (hit@1) | 16057/3004 | 767.0 |
| Kinematics_Basic_8 | correct | verdict | CORRECT |  |  | 10171/2367 | 780.5 |
| Kinematics_Basic_8 | wrong | error |  | Used the river width as the downstream drift. |  | 3551/463 | 771.3 |
| Kinematics_Basic_8 | wrong | verdict | INCORRECT | Added the boat speed and current speed before computing the crossing time. | Added 100 to the correct drift instead of computing it direc (miss) | 12851/2825 | 802.1 |
| Kinematics_Basic_9 | correct | verdict | EQUIVALENT |  |  | 10053/2519 | 800.0 |
| Kinematics_Basic_9 | wrong | verdict | INCORRECT | Forgot that acceleration equals v(dv/dx) and integrated only the right-hand side. | (diagnosis errored) | 15147/3765 | 828.6 |
| Kinematics_Basic_9 | wrong | verdict | INCORRECT | Forgot the factor of 2 when integrating v dv. | (diagnosis errored) | 10708/3268 | 844.1 |

## Errors (crashed trials, isolated)

- `First_Order_ODE_2` (correct): ValueError: verifier: expected tool_use, got 'end_turn'
- `First_Order_ODE_3` (correct): ValueError: verifier: expected tool_use, got 'end_turn'
- `Second_Order_ODE_1` (correct): ValueError: verifier: expected tool_use, got 'end_turn'
- `Second_Order_ODE_1` (wrong): ValueError: verifier: expected tool_use, got 'end_turn'
- `Calculus_Integration_1` (wrong): ValidationError: 1 validation error for Adjudication
suspected_faulty
  Field required [type=missing, input_value={'contradiction': 'Both s...nt_verifier_step': None}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
- `Calculus_Optimization_1` (wrong): ValueError: verifier: expected tool_use, got 'end_turn'
- `Kinematics_1st_1` (wrong): ValueError: ISOLATION BREACH (I1): student-answer fragment '9.8' found in a solver-bound prompt. Refusing to call the model.
- `Kinematics_1st_2` (correct): AttributeError: 'str' object has no attribute 'values'
- `Kinematics_1st_2` (wrong): ValidationError: 3 validation errors for Adjudication
first_divergent_solver_step
  Field required [type=missing, input_value={'contradiction': 'Both s...ted_faulty": "both"}\n'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
first_divergent_verifier_step
  Field required [type=missing, input_value={'contradiction': 'Both s...ted_faulty": "both"}\n'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
suspected_faulty
  Field required [type=missing, input_value={'contradiction': 'Both s...ted_faulty": "both"}\n'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
- `Kinematics_2nd_1` (correct): ValueError: verifier: expected tool_use, got 'end_turn'
- `Kinematics_Basic_4` (wrong): ValueError: ISOLATION BREACH (I1): student-answer fragment '18.0' found in a solver-bound prompt. Refusing to call the model.
- `Kinematics_Basic_5` (wrong): ValidationError: 6 validation errors for StructuredSolution
given
  Field required [type=missing, input_value={'parameter': {'given': [...wer_type': 'symbolic'}}}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
unknowns
  Field required [type=missing, input_value={'parameter': {'given': [...wer_type': 'symbolic'}}}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
method_name
  Field required [type=missing, input_value={'parameter': {'given': [...wer_type': 'symbolic'}}}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
steps
  Field required [type=missing, input_value={'parameter': {'given': [...wer_type': 'symbolic'}}}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
final_answer
  Field required [type=missing, input_value={'parameter': {'given': [...wer_type': 'symbolic'}}}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
parameter
  Extra inputs are not permitted [type=extra_forbidden, input_value={'given': ['m = mass of c...swer_type': 'symbolic'}}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/extra_forbidden
- `Kinematics_Basic_8` (wrong): ValueError: ISOLATION BREACH (I1): student-answer fragment '100.0' found in a solver-bound prompt. Refusing to call the model.

## Caveats

- Verdicts near the 1% physics tolerance boundary (e.g. 3 vs 3.03 s) are policy calls, not math errors; see DEVLOG.
- Token counts run ~30% above pre-migration estimates (new tokenizer).
- Adaptive thinking tokens count against max_tokens and vary per problem, so per-trial cost is not uniform.
- Diagnosis matching is judged SEMANTICALLY by a small model (a model grading a model); hypotheses and causes are in the records JSONL -- spot-check them. Replay verification (mechanically reproducing the student's value from the hypothesized error) is the planned v2 upgrade.