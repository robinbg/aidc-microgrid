"""Real fault dispatch and auditable resilience statistics (SPEC §§3.7, 5).

Critical demand is f_crit*IT_base on the IT side. Faults use service-first
dispatch, never economic re-optimization. Unproven island times are unknown;
electricity and heat remain separate energy quantities.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import warnings

import numpy as np
import pandas as pd

from aidc.model.dispatch import solve_dispatch, summarize_fault_service
from aidc.resilience.scenarios import FaultScenario


@dataclass
class ResilienceResult:
    """Scenario outcome. full_service is critical electricity plus heat.

    full_service_strict additionally requires no noncritical shedding.
    Unproven EENS is an incumbent observation, not an optimality claim.
    """
    scenario_id: str
    reports_t_island: bool
    t_island_h: float | None
    censored: bool | float | None
    eens_mwh: float
    unserved_elec_mwh: float
    unserved_heat_mwh_th: float
    heat_outage_cost_yuan: float
    full_service: bool | float
    full_service_strict: bool | float
    trajectory: pd.DataFrame
    notes: str = ""
    probability: float = 1.0
    counts_toward_n1_pass: bool = False
    scenario_type: str = ""
    status: str = "unknown"
    skipped: bool = False
    skip_reason: str = ""
    level1_proven: bool = False
    eens_critical_mwh: float = float("nan")
    solver_info: dict = field(default_factory=dict)
    initial_soc_source: str = ""
    critical_it_fraction: float = float("nan")
    duration_h: float = float("nan")
    time_step_h: float = 1.0
    renewable_reference: str = ""


def _settings(cfg):
    econ = cfg.get("economics", cfg.get("econ", cfg))
    return econ.get("resilience", econ)


def _number(value, name, nonnegative=False):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not np.isfinite(number) or (nonnegative and number < 0):
        raise ValueError(f"{name} must be finite" + (" and nonnegative" if nonnegative else ""))
    return number


def critical_load_series(ts: pd.DataFrame, tasks: pd.DataFrame, cfg: dict) -> pd.Series:
    """Return f_crit*IT_base, in IT-side MW, ignoring paused task demand.

    Safety/overhead is already included in IT_base. Cooling follows served
    IT through PUE and is not an independent critical-load fraction.
    """
    fraction = _number(_settings(cfg)["critical_it_fraction"], "critical_it_fraction")
    if not 0 <= fraction <= 1:
        raise ValueError("critical_it_fraction must lie in [0, 1]")
    base = ts["IT_base"].to_numpy(dtype=float)
    if not np.isfinite(base).all() or (base < 0).any():
        raise ValueError("IT_base must be finite and nonnegative")
    result = pd.Series(fraction * base, index=ts.index, name="critical_it")
    result.attrs.update(unit="MW", side="IT", critical_it_fraction=fraction)
    return result


def _blank(scenario, status, reason, *, skipped=False, info=None, source="", fraction=float("nan")):
    return ResilienceResult(
        scenario.scenario_id, scenario.reports_t_island,
        float("nan") if scenario.reports_t_island else None,
        float("nan") if scenario.reports_t_island else False,
        float("nan"), float("nan"), float("nan"), float("nan"),
        float("nan"), float("nan"), pd.DataFrame(), reason,
        probability=float(scenario.probability), counts_toward_n1_pass=scenario.counts_toward_n1_pass,
        scenario_type=scenario.type, status=status, skipped=skipped,
        skip_reason=reason if skipped else "", solver_info=info or {}, initial_soc_source=source,
        critical_it_fraction=fraction, duration_h=float(scenario.duration_h),
        renewable_reference=getattr(scenario, "renewable_reference", ""))


def _initial_soc(cfg, ts, sizing):
    """Resolve recorded state, never substitute chemistry defaults."""
    settings = _settings(cfg)
    mode = settings["fault_initial_soc_mode"]
    if mode == "fixed":
        value = _number(settings["fault_initial_soc_value"], "fault_initial_soc_value")
        if not 0 <= value <= 1:
            raise ValueError("fault_initial_soc_value must lie in [0, 1]")
        return "economics.resilience.fault_initial_soc_value"
    if mode != "from_normal_end":
        raise ValueError(f"unknown fault_initial_soc_mode {mode!r}")
    active = [name for name in ("Na", "Li") if sizing[f"E_{name}"] > 0]
    if not active:
        return "not_applicable_no_operational_storage"
    for source, values in [("cfg.initial_soc", cfg.get("initial_soc")),
                           ("ts.attrs.soc_initial", ts.attrs.get("soc_initial")),
                           ("ts.attrs.soc_terminal", ts.attrs.get("soc_terminal"))]:
        if values is None:
            continue
        if not isinstance(values, dict) or any(name not in values for name in active):
            raise ValueError(f"{source} must supply initial SOC for {active}")
        for name in active:
            if not 0 <= _number(values[name], f"{source}.{name}") <= 1:
                raise ValueError(f"{source}.{name}: initial SOC must lie in [0, 1]")
        cfg["initial_soc"] = deepcopy(values)
        return source
    raise ValueError("fault from_normal_end requires explicit initial SOC in cfg.initial_soc, "
                     "ts.attrs.soc_initial, or normal-dispatch ts.attrs.soc_terminal")


def evaluate_scenario(scenario: FaultScenario, ts: pd.DataFrame, tasks: pd.DataFrame,
                      cfg: dict, sizing: dict, seed: int) -> ResilienceResult:
    """Apply an exact interval to copies and invoke real fault dispatch.

    Grid faults remove import/export, not gas. Renewable profiles cap the
    actual trace and cannot create nighttime PV. Nonexistent failed storage
    is skipped. Absent incumbents remain unknown, never zero EENS.
    """
    kinds = {"grid_outage", "grid_outage_extended", "renewable_drop",
             "renewable_drop_plus_grid", "n1_storage", "n1_plus_grid"}
    if scenario.type not in kinds:
        raise ValueError(f"unknown scenario type {scenario.type!r}")
    _number(scenario.probability, "scenario probability", True)
    if not isinstance(ts.index, pd.DatetimeIndex) or ts.empty or ts.index.has_duplicates:
        raise ValueError("fault input requires a nonempty unique DatetimeIndex")
    dt = _number(ts.attrs.get("dt_hours", 1.0), "dt_hours")
    if dt != 1 or (len(ts) > 1 and not (np.diff(ts.index.asi8) == pd.Timedelta(hours=1).value).all()):
        raise ValueError("fault dispatch requires consecutive hourly input")
    start, duration = _number(scenario.start_h, "start_h", True), _number(scenario.duration_h, "duration_h")
    if start != int(start) or duration != int(duration) or duration <= 0:
        raise ValueError("fault start and duration must be whole hours; duration must be positive")
    start, duration = int(start), int(duration)
    if start + duration > len(ts):
        raise ValueError("fault window extends beyond input; no truncation or wrapping is allowed")
    grid_fault = scenario.type in {"grid_outage", "grid_outage_extended", "renewable_drop_plus_grid", "n1_plus_grid"}
    n1_fault = scenario.type in {"n1_storage", "n1_plus_grid"}
    if bool(scenario.reports_t_island) != grid_fault:
        raise ValueError("reports_t_island must match whether the scenario disconnects the grid")
    if bool(scenario.counts_toward_n1_pass) != (scenario.type == "n1_storage"):
        raise ValueError("only standalone N-1 scenarios count toward n1_pass_rate")
    work_cfg, work_sizing = deepcopy(cfg), deepcopy(sizing)
    for name in ("Na", "Li"):
        for key in (f"E_{name}", f"P_{name}"):
            work_sizing[key] = _number(work_sizing[key], key, True)
    settings = _settings(work_cfg)
    fraction = _number(settings["critical_it_fraction"], "critical_it_fraction")
    if not 0 <= fraction <= 1:
        raise ValueError("critical_it_fraction must lie in [0, 1]")
    if n1_fault:
        if scenario.failed_storage not in ("Na", "Li"):
            raise ValueError("N-1 failed_storage must be Na or Li")
        failed = scenario.failed_storage
        if work_sizing[f"E_{failed}"] == 0:
            return _blank(scenario, "skipped", f"{failed} has zero installed energy; no failed component exists",
                          skipped=True, fraction=fraction)
        work_sizing[f"E_{failed}"] = work_sizing[f"P_{failed}"] = 0.0
    source = _initial_soc(work_cfg, ts, work_sizing)
    window = ts.iloc[start:start + duration].copy(deep=True)
    window.attrs = deepcopy(ts.attrs)
    window.attrs.update(dt_hours=dt, scenario_id=scenario.scenario_id,
                        scenario_start_h=start, fault_initial_soc_source=source)
    if grid_fault:
        outage = duration if scenario.outage_hours is None else _number(scenario.outage_hours, "outage_hours")
        if outage != duration:
            raise ValueError("grid-fault outage_hours must equal its exact observation duration")
        window["g_imp_capacity"] = 0.0
        window["g_exp_capacity"] = 0.0
    if scenario.type in {"renewable_drop", "renewable_drop_plus_grid"}:
        profiles = {"P_pv_avail": getattr(scenario, "renewable_pv_mw", None),
                    "P_wind_avail": getattr(scenario, "renewable_wind_mw", None)}
        has_profiles = any(profile is not None for profile in profiles.values())
        if has_profiles and not all(profile is not None for profile in profiles.values()):
            raise ValueError("renewable drop requires both PV and wind profiles")
        if has_profiles:
            for col, profile in profiles.items():
                values = np.asarray(profile, dtype=float)
                if values.shape != (duration,) or not np.isfinite(values).all() or (values < 0).any():
                    raise ValueError(f"{col} fault profile must have {duration} finite nonnegative values")
                window[col] = np.minimum(window[col].to_numpy(dtype=float), values)
        elif scenario.renewable_scale is not None:
            scale = _number(scenario.renewable_scale, "renewable_scale")
            if not 0 <= scale <= 1:
                raise ValueError("renewable_scale must lie in [0, 1]")
            window[["P_pv_avail", "P_wind_avail"]] *= scale
        else:
            raise ValueError("renewable drop requires explicit low-percentile profiles or renewable_scale")
    result = solve_dispatch(window, tasks.copy(deep=True), work_cfg, work_sizing,
                            strategy="S-cost", seed=seed, mode="fault")
    if result.trajectories.empty:
        return _blank(scenario, result.status, "No feasible fault trajectory returned",
                      info=result.solver_info, source=source, fraction=fraction)
    trajectory = result.trajectories.copy(deep=True)
    trajectory.attrs = deepcopy(result.trajectories.attrs)
    proven = bool(result.solver_info.get("level1_proven", False))
    service = summarize_fault_service(trajectory, work_cfg, level1_proven=proven)
    unserved = trajectory["unserved_elec"].to_numpy(dtype=float)
    heat = trajectory["unserved_heat"].to_numpy(dtype=float)
    critical = trajectory["PUE"].to_numpy(dtype=float) * trajectory["shed_c"].to_numpy(dtype=float)
    if any(not np.isfinite(values).all() or (values < -1e-7).any() for values in (unserved, heat, critical)):
        raise ValueError("fault trajectory contains nonfinite or negative unserved energy")
    eens, heat_energy, critical_energy = (float(np.maximum(values, 0).sum() * dt)
                                         for values in (unserved, heat, critical))
    heat_cost = _number(settings["heat_outage_cost_per_mwh"], "heat_outage_cost_per_mwh", True)
    trajectory.attrs.update(scenario_id=scenario.scenario_id, scenario_type=scenario.type,
                            scenario_probability=float(scenario.probability),
                            fault_initial_soc_source=source, critical_it_fraction=fraction,
                            resilience_basis="service-first fault dispatch; not an economic optimum")
    notes = "Fault service lexicographic dispatch; tasks paused; gas unchanged; all-IT bus-side EENS, separate heat. "
    if not proven:
        notes += "First-stage optimum unproven; EENS is incumbent-dependent; island/censoring unknown."
    elif scenario.reports_t_island and service["censored"]:
        notes += "Right-censored island time is a lower bound."
    return ResilienceResult(
        scenario.scenario_id, scenario.reports_t_island,
        service["t_island_h"] if scenario.reports_t_island else None,
        service["censored"] if scenario.reports_t_island else False,
        eens, eens, heat_energy, heat_energy * heat_cost,
        service["full_service"], service["full_service_strict"], trajectory, notes,
        probability=float(scenario.probability), counts_toward_n1_pass=scenario.counts_toward_n1_pass,
        scenario_type=scenario.type, status=result.status, level1_proven=proven,
        eens_critical_mwh=critical_energy, solver_info=deepcopy(result.solver_info),
        initial_soc_source=source, critical_it_fraction=fraction, duration_h=float(duration),
        time_step_h=dt, renewable_reference=getattr(scenario, "renewable_reference", ""))


def aggregate_resilience(results: list[ResilienceResult]) -> dict:
    """Aggregate separate electrical/heat energies and island lower bounds.

    Nonexistent failed components are skipped with renormalized weights. A
    missing trajectory makes EENS unknown, never a zero or silent omission.
    Unproven incumbents contribute observed EENS with an explicit caveat but
    are excluded from island means/censoring. Island weights are separately
    normalized. Count-based censoring/N-1 rates and weighted variants coexist.
    """
    if len({r.scenario_id for r in results}) != len(results):
        raise ValueError("scenario_id must be unique for aggregation")
    probabilities = np.asarray([_number(r.probability, "scenario probability", True) for r in results])
    total_mass = float(probabilities.sum())
    if results and total_mass <= 0:
        raise ValueError("scenario probabilities must have positive total mass")
    active = np.asarray([not r.skipped for r in results], dtype=bool)
    active_mass = float(probabilities[active].sum())
    weights = probabilities / active_mass if active_mass else np.zeros(len(results))
    weights[~active] = 0.0
    rows = []
    for r, weight in zip(results, weights, strict=True):
        row = {name: getattr(r, name) for name in (
            "scenario_id", "scenario_type", "probability", "status", "skipped", "skip_reason",
            "reports_t_island", "counts_toward_n1_pass", "t_island_h", "censored", "eens_mwh",
            "eens_critical_mwh", "unserved_elec_mwh", "unserved_heat_mwh_th", "heat_outage_cost_yuan",
            "full_service", "full_service_strict", "level1_proven", "initial_soc_source",
            "critical_it_fraction", "duration_h", "renewable_reference", "notes")}
        row.update(effective_probability=float(weight), solver_backend=r.solver_info.get("backend"),
                   solver_time_s=r.solver_info.get("time_s"), solver_mip_gap=r.solver_info.get("mip_gap"),
                   level_statuses=r.solver_info.get("level_statuses", []))
        rows.append(row)
    included = [r for r in results if not r.skipped]
    island = [i for i, r in enumerate(results) if active[i] and r.reports_t_island and r.level1_proven
              and r.t_island_h is not None and np.isfinite(r.t_island_h)
              and isinstance(r.censored, (bool, np.bool_))]
    unproven = [r for r in included if not r.level1_proven and np.isfinite(r.eens_mwh)]
    n_failed = sum(not np.isfinite(r.eens_mwh) for r in included)
    n1 = [i for i, r in enumerate(results) if active[i] and r.counts_toward_n1_pass]

    def weighted_attribute(attribute):
        values = np.asarray([getattr(r, attribute) for r in results], dtype=float)
        relevant = active & (probabilities > 0)
        if not relevant.any() or not np.isfinite(values[relevant]).all():
            return float("nan")
        return float(weights[relevant] @ values[relevant])

    if island and probabilities[island].sum() > 0:
        w = probabilities[island] / probabilities[island].sum()
        times = np.asarray([results[i].t_island_h for i in island])
        censor = np.asarray([results[i].censored for i in island], dtype=float)
        time_mean, worst = float(w @ times), float(times.min())
        censor_rate, weighted_censor = float(censor.mean()), float(w @ censor)
    else:
        time_mean = worst = censor_rate = weighted_censor = float("nan")
    if n1:
        services = np.asarray([results[i].full_service for i in n1], dtype=float)
        if np.isfinite(services).all():
            n1_rate = float(services.mean())
            mass = probabilities[n1].sum()
            weighted_n1 = float(probabilities[n1] @ services / mass) if mass else float("nan")
        else:
            n1_rate = weighted_n1 = float("nan")
    else:
        n1_rate = weighted_n1 = float("nan")
    unproven_rate = len(unproven) / len(included) if included else float("nan")
    messages = []
    if censor_rate > .5:
        messages.append("More than 50% of proven island observations are right-censored; extend outage duration.")
    if unproven_rate > .2:
        messages.append("More than 20% of active scenarios lack a first-stage optimality proof; reduce scope or use rolling horizon, not a looser gap.")
    if n_failed:
        messages.append("At least one active scenario has no feasible trajectory; aggregate energy metrics are unknown.")
    for message in messages:
        warnings.warn(message, RuntimeWarning, stacklevel=2)
    return {
        "t_island_mean": time_mean, "t_island_worst": worst,
        "t_island_mean_is_lower_bound": bool(island and any(results[i].censored for i in island)),
        "censoring_rate": censor_rate, "weighted_censoring_rate": weighted_censor,
        "eens": weighted_attribute("eens_mwh"), "eens_critical": weighted_attribute("eens_critical_mwh"),
        "eens_heat": weighted_attribute("unserved_heat_mwh_th"),
        "heat_outage_cost_yuan": weighted_attribute("heat_outage_cost_yuan"),
        "n1_pass_rate": n1_rate, "weighted_n1_pass_rate": weighted_n1,
        "n_scenarios": len(results), "n_active": len(included), "n_skipped": len(results)-len(included),
        "n_failed": n_failed, "n_unproven": len(unproven), "unproven_rate": unproven_rate,
        "n_t_island": len(island), "n_n1": len(n1), "probability_mass": total_mass,
        "active_probability_mass": active_mass, "t_island_probability_mass": float(probabilities[island].sum()),
        "eens_includes_unproven_incumbents": bool(unproven),
        "aggregation_basis": "Count censoring/N-1 rates; conditional probability-weighted island lower-bound mean; renormalized non-skipped EENS probabilities.",
        "warnings": messages, "by_scenario": pd.DataFrame(rows),
    }
