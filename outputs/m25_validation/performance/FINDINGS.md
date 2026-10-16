# Full-horizon S-cost performance experiments

All experiments retain the same 168-hour R-NW input, 2,200 original tasks,
A3 sizing, original objective, one solver thread and requested MIP gap 0.01.
Production source and YAML files were not changed by these scripts.

| Experiment | Primal objective | Dual bound | Actual relative gap | Outcome |
|---|---:|---:|---:|---|
| Accepted M2 original | 3,585,048.76 | 558,473.65 | 84.42% | Feasible, insufficient accuracy |
| Verified S-base feasible primary cutoff | 581,560.95 | 567,866.43 | 2.3548% | Real improvement; still above 1% |
| Explicit objective-bound fixing of 14,388 late starts | 581,560.95 | 558,503.90 | 3.9647% | No further improvement |
| Equal-work task order cuts | none | none | none | No solution at 60 seconds |
| Integer task counts plus continuous assignment flow | none | none | none | No solution at 60 seconds |
| Full original LP relaxation, diagnostic only | none | none | none | LP root not completed at 60 seconds |
| Compact task counts + separated Hall cuts + genuine HiGHS warm start | 581,560.95 | -1,132.79 | 100.19% | Restored original integer solution; weak bound |
| Compact task counts + complete sparse prefix Hall constraints | none | none | none | No solution at 60 seconds |
| Refined incumbent via real HiGHS MIP start, 30-second total budget | 572,409.25 | -182,986.75 | 131.97% | Kept valid seed; new bound weak |
| Fresh fixed-task feasible candidate + physical job-energy relaxation certificate | 572,360.55 | 570,918.42 | 0.251962% | Certified gap for the original 168-hour model |

The independently generated S-base candidate has combined cost 608,792.9451,
zero SLA penalty and exact original integrality. Its original-model constraint
residual is at most 3.55e-15. A feasible upper bound does not remove an optimum:
only solutions worse than an actually feasible candidate are excluded.

The interval objective lower bound is -26,880. Any late binary equal to one
forces cost at least 973,645.17. Thus lateness can be eliminated **for this input**
without overriding the SLA trade-off. This proof must be recomputed per input;
it is not a universal hard zero-lateness constraint.

There are only 22 duplicate complete task windows (2,178 distinct windows), so
simple duplicate aggregation cannot meaningfully reduce this problem. There
are 26 equal-(GPU,duration) groups. Count aggregation is exact only after late
starts have been removed by the bound proof and each count solution has an
integer task-to-slot matching. The prototype successfully reconstructed a
full 76,884-variable solution with zero original integrality residual, maximum
original-row residual 7.11e-15, and objective difference 1.16e-10. It is **not**
an LP substitute or an accepted faster production solver.

The complete prefix-Hall model has 12,892 variables and 55,526 constraints;
HiGHS presolve takes less than one second and reduces it to 8,397 variables
and 16,540 constraints. A short verbose run reached 4,508 root-LP iterations
in five seconds without a primal solution. The root relaxation is the observed
bottleneck, rather than Python construction or presolve alone.

The original cost is dominated by curtailment and heat after lateness is fixed:
the 168-hour wind mean is 17.2269 MW, while the installation's IT scale is only
4.3 MW. The configured curtailment penalty is 200 yuan/MWh. The physical storage
charge/discharge disjunction therefore remains economically active.

The successful certificate uses a **different bound-only model**, retaining all
504 physical binary variables and relaxing task timing to cumulative necessary
energy constraints. It has 4,707 variables and 3,866 rows. Its actual HiGHS dual
bound is 570,918.415821; its own feasible objective is not used as a bound and
its trajectory is never reported as a dispatch. The bound solve took 20.799
seconds; the standalone bound script including input loading/model setup took
26.952 seconds. The primal 572,360.547133 is a separately validated, genuinely
integer 2,200-task solution of the original model. Their gap is therefore an
original-model certificate, not a conditional fixed-task gap.

The independent `solve_cost_lower_bound` helper implements this proof in
`src/aidc/model/_cost_bound.py`, with five passing unit/real-MILP checks in
`tests/test_cost_bound.py`. End-to-end per-call time and generalisation to other
regions/sizings still need the parent integration run; this one data point does
not certify all future BO candidates.

Artifacts retain each failed experiment. A diagnostic LP is never exported
or described as a valid dispatch.
