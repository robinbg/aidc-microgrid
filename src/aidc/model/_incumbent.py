"""Safe objective cutoff from an independently validated feasible schedule.

This does not fix the baseline's task decisions or relax integrality. If x0 is
feasible, any optimum of c*x satisfies c*x <= c*x0. The additive allowance is
only numerical slack; negative objective values use the same inequality.
"""
from copy import copy
from time import perf_counter

import numpy as np

from aidc.model._baseline import build_baseline_outcome


def _merge_lower_bounds(upper, bounds):
    """Discard inconsistent/nonfinite certificates before computing a gap."""
    valid = [float(value) for value in bounds if value is not None and np.isfinite(value)
             and value <= upper + 1e-6]
    if not valid:
        return float("nan"), float("nan")
    lower = min(float(upper), max(valid))
    return lower, max(0., (upper - lower) / max(abs(upper), 1e-10))


def add_feasible_cost_cut(model, objective, *, deadline=None, run_milp=None):
    """Try deterministic rules on the original feasible set, then add a cut.

    A rule failure says nothing about MILP feasibility and causes no cut. No
    objective values from a different instance or strategy are reused.
    """
    started = perf_counter()
    audit = {"applied": False, "source": "same-instance deterministic feasible schedule",
             "preserves": "full horizon, objective, integrality and globally optimal solutions"}
    if model.meta["mode"] != "normal" or model.meta["strategy"] != "S-cost":
        audit.update(reason="only normal S-cost is enabled", preparation_s=perf_counter() - started)
        return audit
    candidate_model = copy(model)
    candidate_model.meta = dict(model.meta, strategy="S-base")
    candidate = build_baseline_outcome(candidate_model)
    if candidate.x is None:
        audit.update(reason="deterministic rules found no feasible candidate; MILP remains unchanged",
                     candidate_status=candidate.status, preparation_s=perf_counter() - started)
        return audit
    x = np.asarray(candidate.x, dtype=float)
    c = np.asarray(objective, dtype=float)
    constraint = model.constraint()
    values = constraint.A @ x
    bounds_error = float(np.maximum(np.asarray(model.lower) - x, x - np.asarray(model.upper)).max(initial=0.))
    row_error = float(np.maximum(constraint.lb - values, values - constraint.ub).max(initial=0.))
    integer = np.asarray(model.integrality, dtype=bool)
    integer_error = float(np.abs(x[integer] - np.rint(x[integer])).max(initial=0.))
    if max(bounds_error, row_error) > 1e-7 or integer_error > 1e-9:
        audit.update(reason="candidate failed independent original-model validation",
                     bound_residual=bounds_error, row_residual=row_error,
                     integer_residual=integer_error, preparation_s=perf_counter() - started)
        return audit
    if deadline is not None and run_milp is not None:
        from aidc.model._incumbent_search import improve_task_incumbent
        proposed, search = improve_task_incumbent(model, x, deadline=deadline, run_milp=run_milp)
        audit["search"] = search
        if proposed.shape == x.shape and np.isfinite(proposed).all():
            values = constraint.A @ proposed
            new_bounds = float(np.maximum(np.asarray(model.lower) - proposed, proposed - np.asarray(model.upper)).max(initial=0.))
            new_rows = float(np.maximum(constraint.lb - values, values - constraint.ub).max(initial=0.))
            new_integers = float(np.abs(proposed[integer] - np.rint(proposed[integer])).max(initial=0.))
            if max(new_bounds, new_rows) <= 1e-7 and new_integers <= 1e-9 and c @ proposed <= c @ x:
                x = proposed
                bounds_error, row_error, integer_error = new_bounds, new_rows, new_integers
            else:
                audit["search_rejected_by_cut_validation"] = True
        else:
            audit["search_rejected_by_cut_validation"] = True
    bound = float(c @ x)
    primary = np.asarray(model.objective_components["primary"], dtype=float)
    remainder = c - primary
    positive, negative = remainder > 0, remainder < 0
    remainder_lower = float(remainder[positive] @ np.asarray(model.lower)[positive]
                            + remainder[negative] @ np.asarray(model.upper)[negative])
    if not np.isfinite(remainder_lower):
        audit.update(reason="objective remainder has no finite interval lower bound",
                     preparation_s=perf_counter() - started)
        return audit
    allowance = 1e-6
    # If primary(x)+remainder(x) <= bound, primary(x) <= bound-min(remainder).
    # Keeping the tiny auxiliary coefficients out of this row avoids a badly
    # scaled new constraint without removing any better combined-objective x.
    rhs = bound - remainder_lower + allowance
    model.row("validated_feasible_objective_cutoff", enumerate(primary), upper=rhs)
    model.meta["_normal_feasible_incumbent"] = x.copy()
    audit.update(applied=True, feasible_objective=bound,
                 objective_constant=float(model.objective_constant), additive_allowance=allowance,
                 cutoff_rhs=rhs, cutoff_basis="primary <= feasible combined objective - interval remainder lower bound",
                 remainder_lower_bound=remainder_lower, bound_residual=bounds_error,
                 row_residual=row_error, integer_residual=integer_error,
                 preparation_s=perf_counter() - started)
    return audit
