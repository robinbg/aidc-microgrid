"""Bounded feasible-incumbent search for the unchanged full dispatch model.

Whole-task moves and a conditional physical solve produce only a feasible
upper bound. The restricted solve's dual bound/gap never certify the original
problem. No files, global caches, input mutation, or new dispatch model builds.
"""
from __future__ import annotations

from copy import copy, deepcopy
from time import perf_counter

import numpy as np


def _residuals(model, x, constraint):
    lower, upper = np.asarray(model.lower), np.asarray(model.upper)
    integer = np.asarray(model.integrality, dtype=bool)
    ax = constraint.A @ x
    return {
        "bounds": float(np.maximum(lower-x, x-upper).max(initial=0)),
        "rows": float(np.maximum(constraint.lb-ax, ax-constraint.ub).max(initial=0)),
        "integrality": float(np.abs(x[integer]-np.rint(x[integer])).max(initial=0)),
    }


def _finite_or_none(value):
    return float(value) if value is not None and np.isfinite(value) else None


def improve_task_incumbent(model, seed_x, *, deadline, run_milp):
    """Return an improved original-model feasible vector, or the seed unchanged.

    ``deadline`` is an absolute ``perf_counter`` deadline. Work is capped at
    min(20 seconds, 40% of remaining caller time); local search uses at most
    three seconds and the one physical solve at most twelve seconds. The
    injected runner has signature ``run_milp(model, objective, seconds, gap)``.

    Only normal S-cost/weighted-single-solve is supported. The surrogate uses
    configured COP/recovery, municipal and industrial heat, gas cost, grid
    purchases and curtailment. It does not claim to optimize storage. Every
    accepted vector passes all original bounds, integer and row checks and
    improves the actual combined objective. Failure returns the original seed.
    """
    started = perf_counter()
    if not np.isfinite(deadline):
        raise ValueError("deadline must be a finite perf_counter timestamp")
    original_seed = np.asarray(seed_x, dtype=float).copy()
    if original_seed.shape != (len(model.variable_names),) or not np.isfinite(original_seed).all():
        raise ValueError("seed_x must be a finite full-model vector")
    budget = min(20.0, max(0.0, float(deadline)-started)*.4)
    stop = started+budget
    diagnostics = {
        "accepted": False, "budget_s": budget, "task_moves": 0,
        "scope": "feasible incumbent only; conditional physical solve is not a global certificate",
        "global_mip_gap": None, "conditional_gap_is_global": False,
    }

    def finish(reason, result=None):
        diagnostics.update(reason=reason, elapsed_s=perf_counter()-started)
        return (original_seed.copy() if result is None else result.copy()), diagnostics

    m = model.meta
    if m["mode"] != "normal" or m["strategy"] != "S-cost":
        return finish("unsupported strategy or trajectory mode")
    if m["experiments"]["auxiliary_lexicographic_term"]["method"] != "weighted_single_solve":
        return finish("unsupported auxiliary objective method")
    if budget <= 1.5:
        return finish("insufficient remaining caller budget")
    constraint = model.constraint()
    validation = _residuals(model, original_seed, constraint)
    diagnostics["seed_validation"] = validation
    if max(validation.values()) > 1e-6:
        raise ValueError(f"seed_x violates the original model: {validation}")
    objective = model.objective_components["primary"]+model.objective_components["auxiliary"]
    constant = float(model.objective_constant)
    seed_value = float(objective @ original_seed)+constant
    diagnostics["seed_objective"] = seed_value
    infos = m["task_info"]
    starts, allowed = [], []
    lower, upper = np.asarray(model.lower), np.asarray(model.upper)
    for info in infos:
        index = int(np.argmax(original_seed[info["indices"]]))
        start = int(info["starts"][index])
        if start+info["duration"] > info["row"]["deadline_h"]:
            return finish("seed includes late tasks; preserve it without heuristic rematching")
        choices = ((upper[info["indices"]] >= 1.-1e-9)
                   & (info["starts"]+info["duration"] <= info["row"]["deadline_h"]))
        forced = lower[info["indices"]] > 1e-9
        if forced.any():
            choices &= forced
        candidates = info["starts"][choices]
        if start not in candidates:
            return finish("seed start outside remaining allowed interval")
        starts.append(start)
        allowed.append(candidates)
    usage = np.zeros(m["T"])
    for start, info in zip(starts, infos):
        usage[start:start+info["duration"]] += info["gpu"]
    multiplier = m["cop"]/(m["cop"]-1)*m["heat_factor"]
    renewable = m["pv"]+m["wind"]

    def proxy(gpu):
        it = m["base"]+gpu*m["p_card"]
        heat = np.minimum(multiplier*it, m["demand"]+m["ind_capacity"])
        municipal = np.minimum(heat, m["demand"])
        bus = m["pue"]*it+heat/m["cop"]
        return (m["price"]*np.maximum(0, bus-renewable)
                + m["boiler_cost"]*(m["demand"]-municipal)
                - m["mun_price"]*municipal-m["ind_price"]*(heat-municipal)
                + m["economics"]["penalties"]["pen_curtail"]*np.maximum(0, renewable-bus))

    diagnostics["surrogate_initial"] = float(proxy(usage).sum())
    order = sorted(range(len(infos)), key=lambda i: (-infos[i]["gpu"]*infos[i]["duration"],
                   infos[i]["row"]["deadline_h"], str(infos[i]["row"]["task_id"])))
    search_stop = min(started+3.0, stop-1.5)
    for _ in range(3):
        changed = 0
        for i in order:
            if perf_counter() >= search_stop:
                break
            info, previous = infos[i], starts[i]
            gpu, duration = info["gpu"], info["duration"]
            usage[previous:previous+duration] -= gpu
            marginal = proxy(usage+gpu)-proxy(usage)
            prefix = np.r_[0., np.cumsum(marginal)]
            costs = prefix[duration:]-prefix[:-duration]
            peak = np.lib.stride_tricks.sliding_window_view(usage, duration).max(axis=1)
            candidates = allowed[i][peak[allowed[i]]+gpu <= m["plan"]+1e-9]
            chosen = int(candidates[np.argmin(costs[candidates])])
            if costs[chosen] >= costs[previous]-1e-8:
                chosen = previous
            else:
                changed += 1
            starts[i] = chosen
            usage[chosen:chosen+duration] += gpu
        diagnostics["task_moves"] += changed
        if not changed or perf_counter() >= search_stop:
            break
    diagnostics["surrogate_final"] = float(proxy(usage).sum())
    restricted = copy(model)
    restricted.lower, restricted.upper = model.lower.copy(), model.upper.copy()
    restricted.meta = deepcopy(model.meta)
    for start, info in zip(starts, infos):
        restricted.fix(info["indices"], 0.)
        restricted.fix(info["indices"][np.flatnonzero(info["starts"] == start)[0]], 1.)
    solve_seconds = min(12.0, stop-perf_counter()-1.0)
    if solve_seconds <= 0:
        return finish("candidate preparation used allocated time")
    diagnostics["physical_time_limit_s"] = solve_seconds
    diagnostics["physical_requested_gap"] = .001
    try:
        result = run_milp(restricted, objective, solve_seconds, .001)
    except (RuntimeError, ValueError) as exc:
        diagnostics["runner_error"] = f"{type(exc).__name__}: {exc}"
        return finish("physical solver failed; retained verified seed")
    diagnostics["physical_solver_status"] = int(result.status)
    diagnostics["conditional_physical_gap"] = _finite_or_none(getattr(result, "mip_gap", None))
    if result.x is None:
        return finish("physical solver returned no candidate")
    candidate = np.asarray(result.x, dtype=float)
    if candidate.shape != original_seed.shape or not np.isfinite(candidate).all():
        return finish("physical solver returned an invalid vector")
    validation = _residuals(model, candidate, constraint)
    diagnostics["candidate_validation"] = validation
    value = float(objective @ candidate)+constant
    diagnostics["candidate_objective"] = value
    if max(validation.values()) > 1e-6:
        return finish("candidate violates original constraints; retained verified seed")
    if value >= seed_value-1e-6:
        return finish("candidate did not improve original objective")
    diagnostics.update(accepted=True, improvement=seed_value-value)
    return finish("improved feasible incumbent", candidate)
