"""Deterministic S-base rules for SPEC 3.5; no optimisation backend.

The shared dispatcher builds the variable catalogue and checks the same physical
rows, but this module supplies every value by FCFS and energy-accounting rules.
All energy values are already in internal MWh units.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .thermal import allocate_heat, heat_source_balance, hp_power_limit


@dataclass
class BaselineOutcome:
    """A rule result, deliberately distinct from an optimiser result."""

    status: str
    x: np.ndarray | None
    diagnostics: dict
    message: str


class _RuleInfeasible(Exception):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.reason = reason
        self.details = details


def _fcfs(meta, x):
    """Place whole tasks in earliest GPU-feasible starts, with stable ties."""
    occupied = np.zeros(meta["T"])
    load = np.asarray(meta["base"], dtype=float).copy()
    infos = meta["task_info"]
    order = sorted(range(len(infos)), key=lambda i: infos[i]["row"]["arrive_h"])
    starts = {}
    for i in order:
        info = infos[i]
        for j, start in enumerate(info["starts"]):
            end = int(start) + info["duration"]
            if np.all(occupied[int(start):end] + info["gpu"] <= meta["plan"]):
                occupied[int(start):end] += info["gpu"]
                load[int(start):end] += info["gpu"] * meta["p_card"]
                x[info["indices"][j]] = 1.0
                starts[info["row"]["task_id"]] = int(start)
                break
        else:
            raise _RuleInfeasible("FCFS task has no GPU-feasible start", task_id=info["row"]["task_id"])
    return load, starts


def _charge_plan(storage, discharge, surplus, grid_room, price, dt, charge_order):
    """Account for each chemistry in battery MWh, then convert at each slot.

    Prefix lower bounds keep every scheduled discharge physically available.
    Terminal balancing uses remaining renewable surplus before grid supply;
    grid slots are sorted by purchase price and then hour.  Each addition is
    limited by all affected SOC upper bounds, so precharging never assumes an
    empty battery or creates a simultaneous charge/discharge.
    """
    horizon = len(price)
    remaining_surplus = surplus * dt
    remaining_grid = grid_room * dt
    charges, renewable_charges, grid_charges, socs = {}, {}, {}, {}
    for name in charge_order:
        st = storage[name]
        E = st["E"]
        initial = float(st["initial"])
        d = discharge[name] * dt
        b = np.zeros(horizon)  # battery-side charge, not bus-side energy
        re = np.zeros(horizon)
        grid = np.zeros(horizon)
        if E == 0:
            if np.any(d > 1e-9):
                raise _RuleInfeasible("zero-capacity storage cannot discharge", chemistry=name)
            charges[name], renewable_charges[name], grid_charges[name] = b.copy(), re, grid
            socs[name] = np.full(horizon + 1, initial)
            continue
        withdrawals = d / st["eta_dis"]
        needed = float(withdrawals.sum())
        ref_energy = initial * E
        # A final charge must replace exactly the battery energy withdrawn.
        # No gratuitous storage cycling or charging beyond this quota is allowed.
        def energy_path():
            return ref_energy + np.cumsum(b - withdrawals)

        def add_before(deadline, need):
            for channel, available in (("renewable", remaining_surplus), ("grid", remaining_grid)):
                candidates = range(deadline + 1)
                if channel == "grid":
                    candidates = sorted(candidates, key=lambda t: (price[t], t))
                for t in candidates:
                    if need <= 1e-10:
                        return 0.0
                    if d[t] > 1e-10 or available[t] <= 1e-12:
                        continue
                    path = energy_path()
                    start_energy = ref_energy if t == 0 else path[t - 1]
                    eta = st["eta_ch"][t]
                    per_hour = max(0.0, st["ch_max"][t] * dt - (re[t] + grid[t])) * eta
                    own_band = max(0.0, E * st["hi"][t] - start_energy - b[t])
                    # Existing later charges must still end inside their bands;
                    # idle points only require the fixed nominal SOC window.
                    upper = np.full(horizon - t, E * st["hard_hi"])
                    active = b[t:] > 1e-12
                    upper[active] = E * st["hi"][t:][active]
                    future_headroom = max(0.0, float(np.min(upper - path[t:])))
                    amount = min(need, available[t] * eta, per_hour, own_band, future_headroom)
                    if amount <= 1e-12:
                        continue
                    b[t] += amount
                    bus = amount / eta
                    available[t] -= bus
                    (re if channel == "renewable" else grid)[t] += bus
                    need -= amount
            return max(0.0, need)

        for t in range(horizon):
            path = energy_path()
            floor = E * (st["lo"][t] if d[t] > 1e-10 else st["hard_lo"])
            deficit = max(0.0, floor - path[t])
            if deficit > 1e-9:
                unfilled = add_before(t, deficit)
                if unfilled > 1e-8:
                    raise _RuleInfeasible("insufficient energy before forced discharge", chemistry=name,
                                          hour=t, missing_battery_mwh=unfilled)
        unfilled = add_before(horizon - 1, max(0.0, needed - b.sum()))
        if unfilled > 1e-8:
            raise _RuleInfeasible("insufficient charging headroom for terminal balance", chemistry=name,
                                  missing_battery_mwh=unfilled)
        soc = np.r_[initial, energy_path() / E]
        if abs(soc[-1] - initial) > 1e-9:
            raise _RuleInfeasible("terminal SOC accounting mismatch", chemistry=name, terminal_soc=float(soc[-1]))
        charges[name] = (re + grid) / dt
        renewable_charges[name], grid_charges[name], socs[name] = re, grid, soc
    return charges, renewable_charges, grid_charges, socs


def build_baseline_outcome(model):
    """Fill a shared dispatch model by deterministic rules, never call MILP.

    An infeasible rule outcome contains diagnostics and no normal trajectory.
    A feasible outcome makes no optimality or solver-gap claim.
    """
    m = model.meta
    if m["mode"] != "normal" or m["strategy"] != "S-base":
        raise ValueError("baseline rules require normal S-base model")
    T, dt, v = m["T"], m["dt"], m["variables"]
    x = np.zeros(len(model.variable_names), dtype=float)
    diagnostics = {"backend": "deterministic_fcfs_rules", "optimized": False,
                   "s_base_grid_charge_mwh": 0.0, "s_base_grid_charge_hours": [],
                   "s_base_surplus_charge_mwh": 0.0, "s_base_curtailed_from_soc_cap_mwh": 0.0,
                   "s_base_terminal_reserve_mwh": 0.0, "s_base_forced_discharge_mwh": 0.0}
    try:
        load, starts = _fcfs(m, x)
        q_rec = m["heat_factor"] * load
        hp = hp_power_limit(q_rec, m["cop"], m["demand"], m["ind_capacity"])
        q_hp = hp * m["cop"]
        q_mun, q_ind, q_dump = allocate_heat(q_hp, m["demand"], m["ind_capacity"])
        _, q_unused = heat_source_balance(q_rec, hp, m["cop"])
        boiler = m["demand"] - q_mun
        capacity_boiler = np.asarray(model.upper)[v["Q_boiler"]]
        if np.any(boiler > capacity_boiler + 1e-8):
            raise _RuleInfeasible("municipal heat exceeds boiler capacity", unserved_heat_mwh=float(np.maximum(0, boiler-capacity_boiler).sum()*dt))
        bus = m["pue"] * load + hp
        renewable = m["pv"] + m["wind"]
        deficit = np.maximum(0.0, bus - renewable)
        forced = np.maximum(0.0, deficit - m["import_capacity"])
        surplus = np.maximum(0.0, renewable - bus)
        grid_room = np.maximum(0.0, m["import_capacity"] - deficit)
        storage = {name: dict(st, initial=float(model.lower[st["soc"][0]])) for name, st in m["storage"].items()}
        discharge = {name: np.zeros(T) for name in storage}
        remaining = forced.copy()
        order = m["economics"]["resilience"]["discharge_order"]
        for name in order:
            st = storage[name]
            cap = st["dis_max"] if st["E"] > 0 else np.zeros(T)
            discharge[name] = np.minimum(remaining, cap)
            remaining -= discharge[name]
        if np.any(remaining > 1e-8):
            raise _RuleInfeasible("electrical power capacity insufficient", unserved_elec_mwh=float(remaining.sum()*dt))
        # A power-first assignment is only a provisional split: the preferred
        # chemistry can be energy-limited.  Move its unavailable contribution
        # to the next chemistry in the *same* hour and recompute the exact
        # balancing charges.  This is an explicit deterministic rule, not a
        # relaxed solve or an alternating-hour discharge prohibition.
        preferred, secondary = order
        reallocations = []
        for _ in range(T + 1):
            try:
                try:
                    charges, re_charge, grid_charge, socs = _charge_plan(storage, discharge, surplus, grid_room, m["price"], dt, order)
                except _RuleInfeasible:
                    # The first chemistry must not consume the sole charging
                    # slot usable by the other. A fixed reversed pass retries
                    # their shared resource allocation, without an optimiser.
                    charges, re_charge, grid_charge, socs = _charge_plan(storage, discharge, surplus, grid_room, m["price"], dt, order[::-1])
                break
            except _RuleInfeasible as error:
                if error.details.get("chemistry") != preferred:
                    raise
                limit = error.details.get("hour", T - 1)
                missing = error.details.get("missing_battery_mwh", float("inf"))
                moved = 0.0
                for t in range(limit, -1, -1):
                    spare = storage[secondary]["dis_max"][t] - discharge[secondary][t]
                    if storage[secondary]["E"] <= 0 or spare <= 1e-12:
                        continue
                    amount = min(discharge[preferred][t], spare,
                                 missing * storage[preferred]["eta_dis"][t] / dt)
                    if amount <= 1e-12:
                        continue
                    discharge[preferred][t] -= amount
                    discharge[secondary][t] += amount
                    missing -= amount * dt / storage[preferred]["eta_dis"][t]
                    moved += amount
                    reallocations.append({"hour": t, "from": preferred, "to": secondary, "power_mw": float(amount)})
                    if missing <= 1e-9:
                        break
                if moved <= 1e-12:
                    raise
        else:
            raise _RuleInfeasible("storage energy allocation did not settle", reallocations=reallocations)
        grid_charge_sum = sum(grid_charge.values())
        re_charge_sum = sum(re_charge.values())
        g_imp = np.minimum(deficit, m["import_capacity"]) + grid_charge_sum / dt
        renewable_used = np.minimum(renewable, bus) + re_charge_sum / dt
        pv_used = np.minimum(m["pv"], renewable_used)
        wind_used = renewable_used - pv_used
        values = {"IT_load": load, "IT_served": load, "P_pv_used": pv_used, "P_wind_used": wind_used,
                  "g_imp": g_imp, "g_exp": np.zeros(T), "P_curtail": renewable-renewable_used,
                  "P_hp": hp, "Q_hp": q_hp, "Q_mun_hp": q_mun, "Q_boiler": boiler,
                  "Q_ind": q_ind, "Q_dump": q_dump, "Q_rec": q_rec, "Q_unused": q_unused,
                  "unserved_heat": np.zeros(T), "shed_c": np.zeros(T), "shed_nc": np.zeros(T),
                  "y_heat_alloc": (q_hp >= m["demand"]).astype(float)}
        for name, value in values.items():
            x[v[name]] = value
        for name, st in storage.items():
            x[st["soc"]], x[st["ch"]], x[st["dis"]] = socs[name], charges[name], discharge[name]
            z = (charges[name] > 1e-12).astype(float)
            # The exact positive-part encoding chooses a compatible idle arm.
            idle = (charges[name] <= 1e-12) & (discharge[name] <= 1e-12)
            z[idle & (socs[name][:-1] < st["lo"])] = 1.0
            x[st["z"]] = z
        # Marginal attribution at the actual start-of-hour SOC: remove only
        # S-base's strategy charge quota while retaining physical power, band,
        # efficiency and same-chemistry mutual-exclusion limits.  Total unused
        # renewable power is not evidence that the strategy cap caused it.
        extra_physical_charge = np.zeros(T)
        for name, st in storage.items():
            if st["E"] <= 0:
                continue
            room = st["E"] / st["eta_ch"] * np.maximum(0.0, st["hi"]-socs[name][:-1]) / dt
            physical = np.minimum(st["ch_max"], room)
            extra = np.maximum(0.0, physical-charges[name])
            extra[discharge[name] > 1e-12] = 0.0
            extra_physical_charge += extra
        cap_curtailment = np.minimum(np.maximum(0.0, surplus-re_charge_sum/dt), extra_physical_charge)
        diagnostics.update({"s_base_grid_charge_mwh": float(grid_charge_sum.sum()),
                            "s_base_grid_charge_hours": np.flatnonzero(grid_charge_sum > 1e-10).tolist(),
                            "s_base_grid_charge_by_hour_mwh": grid_charge_sum.tolist(),
                            "s_base_surplus_charge_mwh": float(re_charge_sum.sum()),
                            "s_base_surplus_charge_by_hour_mwh": re_charge_sum.tolist(),
                            "s_base_curtailed_from_soc_cap_mwh": float(cap_curtailment.sum()*dt),
                            "s_base_curtailed_from_soc_cap_by_hour_mwh": (cap_curtailment*dt).tolist(),
                            "s_base_soc_cap_curtailment_scope": "marginal additional feasible charging at actual hourly SOC after removing strategy quota only; not a full counterfactual trajectory",
                            "s_base_terminal_reserve_mwh": float((grid_charge_sum+re_charge_sum).sum()),
                            "s_base_forced_discharge_mwh": float(forced.sum()*dt),
                            "s_base_task_starts": starts,
                            "s_base_storage_reallocations": reallocations,
                            "s_base_energy_accounting": "per-slot battery withdrawals d/eta_dis matched to eta_ch*c"})
        lower, upper = np.asarray(model.lower), np.asarray(model.upper)
        bound_violation = float(np.maximum(lower-x, x-upper).max(initial=0))
        integer_mask = np.asarray(model.integrality, dtype=bool)
        integer_violation = float(np.abs(x[integer_mask]-np.rint(x[integer_mask])).max(initial=0))
        violations = []
        for name, row, lo, hi in zip(model.row_names, model.rows, model.row_lower, model.row_upper, strict=True):
            value = sum(coef*x[j] for j, coef in row.items())
            error = max(0.0, lo-value, value-hi)
            if error > 1e-7:
                violations.append({"row": name, "residual": float(error)})
        diagnostics["s_base_bound_residual"] = bound_violation
        diagnostics["s_base_integer_residual"] = integer_violation
        diagnostics["s_base_model_row_residual"] = max((a["residual"] for a in violations), default=0.0)
        if bound_violation > 1e-7 or integer_violation > 1e-9 or violations:
            raise _RuleInfeasible("deterministic trajectory violates shared physical model", violations=violations[:10], bound_residual=bound_violation)
        return BaselineOutcome("feasible", x, diagnostics, "deterministic FCFS and capacity-gap storage rules; no optimisation performed")
    except _RuleInfeasible as error:
        diagnostics.update({"infeasibility_reason": error.reason, **error.details})
        return BaselineOutcome("infeasible", None, diagnostics, error.reason)
