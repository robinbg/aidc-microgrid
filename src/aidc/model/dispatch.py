"""Hourly MILP dispatch through scipy.optimize.milp's bundled HiGHS.

All integer decisions remain integer. Inputs/outputs are simulations. Normal
strategies and fault lexicographic objectives are separate. The model retains
named variables/rows and objective vectors for independent structure review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Literal
import warnings

import numpy as np
import pandas as pd
import scipy
from scipy.optimize import Bounds, LinearConstraint, OptimizeResult, milp
from scipy.sparse import coo_matrix

from aidc.data.chemistry import ChemistryParams, chemistry_params_from_config, convert_units, storage_temperature
from aidc.model.storage import usable_band

SolveStatus = Literal["optimal", "feasible", "feasible_not_proven", "infeasible", "unbounded", "error"]
TRAJECTORY_COLUMNS = [
    "IT_load", "IT_served", "it_shed", "P_cool", "P_cool_served",
    "P_pv_used", "P_wind_used", "P_curtail", "g_imp", "g_exp",
    "p_ch_Na", "p_dis_Na", "soc_Na", "soc_lo_Na", "soc_hi_Na",
    "p_ch_Li", "p_dis_Li", "soc_Li", "soc_lo_Li", "soc_hi_Li",
    "Q_rec", "Q_low_used", "Q_unused", "P_hp", "Q_hp", "Q_mun_hp",
    "Q_boiler", "Q_mun_demand", "Q_ind", "Q_dump", "y_heat_alloc",
    "carbon_net", "price_buy", "T_amb", "unserved_elec", "unserved_heat",
    "shed_c", "shed_nc",
]


@dataclass
class DispatchResult:
    status: SolveStatus
    objective: float
    trajectories: pd.DataFrame
    metrics: dict
    solver_info: dict = field(default_factory=dict)
    task_schedule: pd.DataFrame | None = None


def _num(value, name, nonnegative=False):
    if isinstance(value, dict):
        value = value["value"] if "value" in value else value["mid"]
    value = float(value)
    if not np.isfinite(value) or (nonnegative and value < 0):
        raise ValueError(f"{name} must be finite" + (" and nonnegative" if nonnegative else ""))
    return value


def _configs(cfg, ts=None):
    system = convert_units(cfg["system"])
    economics = convert_units(cfg.get("economics", cfg.get("econ")))
    experiments = convert_units(cfg.get("experiments", cfg.get("exp")))
    region = cfg.get("region")
    if region is None and ts is not None:
        region = cfg.get("regions", {}).get(ts.attrs.get("region_id"))
    if region is None:
        raise ValueError("provide cfg['region'] or ts.attrs['region_id'] matching cfg['regions']")
    return system, economics, experiments, convert_units(region)


def _chemistry(cfg, name):
    value = cfg[f"chemistry_{name.lower()}"]
    return value if isinstance(value, ChemistryParams) else chemistry_params_from_config(value)


class DispatchModel:
    """Named sparse MILP; public inspection API, never a relaxed solver."""
    def __init__(self):
        self.variable_names = []
        self.variables = {}
        self.lower = []
        self.upper = []
        self.integrality = []
        self.rows = []
        self.row_names = []
        self.row_lower = []
        self.row_upper = []
        self.objective_components = {}
        self.objective_constant = 0.0
        self.meta = {}

    def variable(self, name, count, lower=0.0, upper=np.inf, integer=False):
        start = len(self.variable_names)
        indices = np.arange(start, start + count, dtype=int)
        lo = np.broadcast_to(lower, (count,)).astype(float)
        hi = np.broadcast_to(upper, (count,)).astype(float)
        if np.any(lo > hi) or np.isnan(lo).any() or np.isnan(hi).any():
            raise ValueError(f"invalid bounds for {name}")
        self.variables[name] = indices
        self.variable_names.extend(f"{name}[{i}]" for i in range(count))
        self.lower.extend(lo)
        self.upper.extend(hi)
        self.integrality.extend([int(integer)] * count)
        return indices

    def row(self, name, terms, lower=-np.inf, upper=np.inf):
        combined = {}
        items = terms.items() if isinstance(terms, dict) else terms
        for index, coefficient in items:
            if coefficient:
                combined[int(index)] = combined.get(int(index), 0.0) + float(coefficient)
        self.rows.append(combined)
        self.row_names.append(name)
        self.row_lower.append(float(lower))
        self.row_upper.append(float(upper))

    def equal(self, name, terms, value=0.0):
        self.row(name, terms, value, value)

    def vector(self, terms=()):
        result = np.zeros(len(self.variable_names))
        for indices, coefficients in terms:
            result[indices] += coefficients
        return result

    def constraint(self):
        rr, cc, vv = [], [], []
        for i, row in enumerate(self.rows):
            for j, value in row.items():
                rr.append(i)
                cc.append(j)
                vv.append(value)
        matrix = coo_matrix((vv, (rr, cc)), shape=(len(self.rows), len(self.variable_names))).tocsc()
        return LinearConstraint(matrix, np.asarray(self.row_lower), np.asarray(self.row_upper))

    def fix(self, indices, values):
        for index, value in zip(np.atleast_1d(indices), np.broadcast_to(values, np.shape(np.atleast_1d(indices))), strict=True):
            self.lower[int(index)] = float(value)
            self.upper[int(index)] = float(value)


def _array(ts, name, fallback=None, nonnegative=True):
    if name in ts:
        result = ts[name].to_numpy(dtype=float)
    elif fallback is not None:
        result = np.broadcast_to(fallback, (len(ts),)).astype(float).copy()
    else:
        raise ValueError(f"time series lacks {name}")
    if not np.isfinite(result).all() or (nonnegative and (result < 0).any()):
        raise ValueError(f"{name} must be finite" + (" and nonnegative" if nonnegative else ""))
    return result


def _build_objective_weights(strategy: str, cfg: dict) -> dict:
    """Resolve independent objective forms; reject uncalibrated S-balance."""
    exp = cfg.get("experiments", cfg.get("exp"))
    if strategy not in ("S-cost", "S-carbon", "S-heat", "S-balance", "S-base"):
        raise ValueError(f"unknown strategy {strategy!r}")
    result = {"strategy": strategy, "unit": {"S-cost": "yuan", "S-carbon": "kgCO2",
              "S-heat": "yuan", "S-balance": "dimensionless", "S-base": "rule-driven"}[strategy]}
    if strategy == "S-balance":
        ranges = exp["normalization"]["ranges"]
        weights = next(s["weights"] for s in exp["strategies"] if s["id"] == strategy)
        result["scales"], result["offsets"] = {}, {}
        for component in ("cost", "carbon", "heat"):
            bounds = ranges[component]
            if bounds["min"] is None or bounds["max"] is None:
                raise ValueError(f"S-balance normalization.{component} is not calibrated (min/max null)")
            low = _num(bounds["min"], component)
            high = _num(bounds["max"], component)
            if high <= low:
                raise ValueError(f"S-balance {component} requires max > min")
            result["scales"][component] = _num(weights[component], component, True) / (high - low)
            result["offsets"][component] = -low * result["scales"][component]
        result["w_sla"] = _num(exp["sla"]["w_sla_balance"], "w_sla_balance", True)
    return result


def build_dispatch_model(ts, tasks, cfg, sizing, strategy="S-cost", seed=0, mode="normal"):
    """Build physical MILP with exact start decisions and thermal min encoding.

    Inputs are the shared config bundle plus region, or region_id in ts.attrs.
    Explicit PUE/T_storage columns are authoritative derived input data; absent
    columns are calculated from config. Optional g_imp_capacity/g_exp_capacity/
    q_boiler_capacity columns define fault restrictions without treating every
    fault as a grid outage. Fault initial states must be supplied for the
    configured from_normal_end policy: cfg.initial_soc or ts.attrs.soc_initial.
    """
    if mode not in ("normal", "fault"):
        raise ValueError("mode must be normal or fault")
    if not isinstance(ts.index, pd.DatetimeIndex) or ts.empty or ts.index.has_duplicates:
        raise ValueError("dispatch requires nonempty unique DatetimeIndex")
    if len(ts) > 1 and not (np.diff(ts.index.asi8) == pd.Timedelta(hours=1).value).all():
        raise ValueError("dispatch requires continuous one-hour time steps")
    system, econ, exp, region = _configs(cfg, ts)
    if mode == "normal":
        objective_settings = _build_objective_weights(strategy, cfg)
    else:
        objective_settings = {"strategy": "fault_lexicographic", "unit": "MWh"}
    model = DispatchModel()
    T, dt = len(ts), 1.0
    temp = _array(ts, "T_amb", nonnegative=False)
    pue_points = system["pue"]["points"]
    pue = _array(ts, "PUE", np.interp(temp, [x["T_amb"] for x in pue_points], [x["value"] for x in pue_points]))
    if (pue < 1).any():
        raise ValueError("PUE must be >= 1")
    site = {**system["storage_site_default"], **region.get("storage_site_override", {})}
    temp_storage = _array(ts, "T_storage", storage_temperature(temp, site), nonnegative=False)
    compute = {**system["compute"], **region.get("compute_override", {})}
    total = int(compute["gpu_capacity_total"])
    reserved = round(total * _num(compute["s1_reserved_fraction"], "s1_reserved_fraction"))
    plan = total - reserved
    p_card = _num(compute["gpu_power"], "gpu_power")
    base = _array(ts, "IT_base")
    pv = _array(ts, "P_pv_avail")
    wind = _array(ts, "P_wind_avail")
    price = _array(ts, "price_buy", nonnegative=False)
    sell = _array(ts, "price_sell", econ["electricity"]["price_sell"], nonnegative=False)
    carbon = _array(ts, "CI_grid")
    demand = _array(ts, "Q_mun_demand")
    ind_capacity = _array(ts, "Q_ind_capacity", region["heat"]["q_ind_capacity_mw_th"])
    if mode == "fault":
        ind_capacity[:] = 0  # fault policy supplies municipal demand only.
    import_capacity = _array(ts, "g_imp_capacity", system["grid"]["import_capacity_mw"])
    export_capacity = _array(ts, "g_exp_capacity", system["grid"]["export_capacity_mw"])
    boiler_capacity = _array(ts, "q_boiler_capacity", system["thermal"]["backup_heat"]["q_capacity_mw_th"])
    cop = _num(system["thermal"]["cop"], "COP")
    eta_rec = _num(system["thermal"]["eta_recovery"], "eta_recovery")
    kappa = _num(system["thermal"]["kappa_cooling_heat"], "kappa")
    if cop <= 1 or not 0 <= eta_rec <= 1 or not 0 <= kappa <= 1:
        raise ValueError("requires COP > 1 and eta_recovery/kappa in [0,1]")
    heat_factor = eta_rec * (1 + kappa * (pue - 1))
    full_it = base + (plan * p_card if mode == "normal" else 0)
    U = np.minimum(cop / (cop - 1) * heat_factor * full_it, demand + ind_capacity)
    M1, M2 = np.maximum(0, U - demand), demand.copy()

    def variable(name, upper=np.inf, lower=0.0, integer=False, count=None):
        return model.variable(name, T if count is None else count, lower, upper, integer)
    v = {}
    for name, upper in (("IT_load", full_it), ("IT_served", full_it),
                        ("P_pv_used", pv), ("P_wind_used", wind),
                        ("g_imp", import_capacity), ("g_exp", export_capacity),
                        ("P_curtail", pv + wind), ("P_hp", U / cop), ("Q_hp", U),
                        ("Q_mun_hp", demand), ("Q_boiler", boiler_capacity),
                        ("Q_ind", ind_capacity), ("Q_dump", U),
                        ("Q_rec", heat_factor * full_it), ("Q_unused", heat_factor * full_it),
                        ("unserved_heat", demand if mode == "fault" else 0.0)):
        v[name] = variable(name, upper)
    fraction = _num(econ["resilience"]["critical_it_fraction"], "critical_it_fraction")
    if not 0 <= fraction <= 1:
        raise ValueError("critical_it_fraction must lie in [0,1]")
    v["shed_c"] = variable("shed_c", fraction * base if mode == "fault" else 0.0)
    v["shed_nc"] = variable("shed_nc", (1 - fraction) * base if mode == "fault" else 0.0)
    v["y_heat_alloc"] = variable("y_heat_alloc", 1, integer=True)

    # Exact nonpreemptive y_i(t) is the sum of start_i(s) covering t.
    # A single binary start is selected, so no relaxation or extra y_i array.
    task_info = []
    active_by_t = [[] for _ in range(T)]
    late_terms = []
    task_rows = tasks if mode == "normal" else tasks.iloc[:0]
    required_task_cols = {"task_id", "priority", "gpu", "duration_h", "arrive_h", "deadline_h", "tolerance_h", "latest_h"}
    if len(task_rows) and not required_task_cols.issubset(task_rows):
        raise ValueError(f"tasks lack columns: {required_task_cols - set(task_rows)}")
    if len(task_rows) and not task_rows.task_id.is_unique:
        raise ValueError("task_id must be unique")
    for i, row in enumerate(task_rows.to_dict("records")):
        if row["priority"] not in ("S2", "S3"):
            raise ValueError("task priority must be S2/S3; S1 is already included in IT_base")
        arrive = _num(row["arrive_h"], "arrive_h", True)
        deadline = _num(row["deadline_h"], "deadline_h", True)
        tolerance = _num(row["tolerance_h"], "tolerance_h", True)
        latest_input = _num(row["latest_h"], "latest_h", True)
        if arrive != int(arrive) or deadline != int(deadline):
            raise ValueError("task arrive_h and deadline_h must be integer hours")
        if latest_input != deadline + tolerance:
            raise ValueError("task latest_h must equal deadline_h + tolerance_h")
        duration = int(row["duration_h"])
        gpu = int(row["gpu"])
        if duration < 1 or duration != row["duration_h"] or gpu < 1 or gpu != row["gpu"]:
            raise ValueError("task gpu and duration_h must be positive integers")
        if row.get("priority", "S3") == "S2" and float(row.get("tolerance_h", 0)) != 0:
            raise ValueError("S2 tolerance must be zero")
        if arrive + duration > T or latest_input > T:
            raise ValueError("task arrival/duration and latest_h must stay within the dispatch window")
        latest = min(float(row["latest_h"]), float(row["deadline_h"]) if row.get("priority") == "S2" else float(row["latest_h"]))
        starts = np.arange(max(0, int(np.ceil(row["arrive_h"]))), min(T - duration, int(np.floor(latest - duration))) + 1)
        indices = model.variable(f"start_{i}", len(starts), 0, 1, True)
        model.equal(f"task_once_{i}", [(j, 1) for j in indices], 1)
        for start, j in zip(starts, indices, strict=True):
            for t in range(start, start + duration):
                active_by_t[t].append((int(j), gpu))
            if start + duration > row["deadline_h"]:
                late_terms.append((int(j), 1.0))
        task_info.append({"row": row, "starts": starts, "indices": indices, "duration": duration, "gpu": gpu})
    for t in range(T):
        model.equal(f"IT_demand_{t}", [(v["IT_load"][t], 1)] + [(j, -gpu * p_card) for j, gpu in active_by_t[t]], base[t])
        model.row(f"GPU_capacity_{t}", active_by_t[t], upper=plan)
        model.equal(f"served_chain_{t}", [(v["IT_served"][t], 1), (v["IT_load"][t], -1),
                    (v["shed_c"][t], 1), (v["shed_nc"][t], 1)])
        model.equal(f"renewable_balance_{t}", [(v["P_pv_used"][t], 1), (v["P_wind_used"][t], 1), (v["P_curtail"][t], 1)], pv[t] + wind[t])

    chemistry, storage_data = {}, {}
    area = 0.0
    for name in ("Na", "Li"):
        chem = _chemistry(cfg, name)
        chemistry[name] = chem
        E = _num(sizing[f"E_{name}"], f"E_{name}", True)
        P = _num(sizing[f"P_{name}"], f"P_{name}", True)
        area += E * chem.area_per_mwh
        lo = max(chem.soc_min, _num(system["storage_common"]["soc_min"], "soc_min"))
        hi = min(chem.soc_max, _num(system["storage_common"]["soc_max"], "soc_max"))
        ref = chem.soc_init
        if not lo <= ref <= hi:
            raise ValueError(f"{name}: soc_init outside effective hard bounds")
        band_lo, band_hi = usable_band(ref, chem.gamma(temp_storage), lo, hi)
        ch_max, dis_max = chem.power_bounds(E, P, temp_storage)
        eta_ch, eta_dis = chem.eta_charge(temp_storage), chem.eta_discharge(temp_storage)
        soc = variable(f"soc_{name}", hi, lo, count=T + 1)
        ch = variable(f"p_ch_{name}", ch_max)
        dis = variable(f"p_dis_{name}", dis_max)
        z = variable(f"z_{name}", 1, integer=True)
        initial = ref
        terminal = ref
        terminal_free = False
        if mode == "normal":
            # Optional rolling-horizon SOC carry-over (SPEC §13.1): explicit
            # initial/terminal maps, or a free terminal for intermediate
            # windows. Without cfg["normal_soc_boundary"] the behavior is
            # exactly the single-window contract soc(0) = soc(T) = soc_init.
            boundary = cfg.get("normal_soc_boundary") or {}
            initial_map = boundary.get("initial")
            if initial_map is not None:
                if name not in initial_map:
                    raise ValueError(f"normal_soc_boundary.initial requires {name!r}")
                initial = _num(initial_map[name], f"normal_soc_boundary.initial.{name}")
            terminal_map = boundary.get("terminal")
            terminal_free = bool(boundary.get("terminal_free", False))
            if terminal_free and terminal_map is not None:
                raise ValueError("normal_soc_boundary: terminal and terminal_free are exclusive")
            if terminal_map is not None:
                if name not in terminal_map:
                    raise ValueError(f"normal_soc_boundary.terminal requires {name!r}")
                terminal = _num(terminal_map[name], f"normal_soc_boundary.terminal.{name}")
        if mode == "fault" and E > 0:
            policy = econ["resilience"]["fault_initial_soc_mode"]
            if policy == "fixed":
                initial = _num(econ["resilience"]["fault_initial_soc_value"], "fault_initial_soc_value")
            elif policy == "from_normal_end":
                initial_map = cfg.get("initial_soc", ts.attrs.get("soc_initial"))
                if initial_map is None or name not in initial_map:
                    raise ValueError(f"fault from_normal_end requires initial_soc[{name!r}]")
                initial = _num(initial_map[name], f"initial_soc.{name}")
            else:
                raise ValueError(f"unknown fault_initial_soc_mode {policy!r}")
        if not lo <= initial <= hi:
            raise ValueError(f"{name}: initial SOC outside hard bounds")
        model.fix(soc[0], initial)
        if mode == "normal":
            if not lo <= terminal <= hi:
                raise ValueError(f"{name}: terminal SOC outside hard bounds")
            if not terminal_free:
                model.fix(soc[-1], terminal)
        for t in range(T):
            model.row(f"charge_mutex_{name}_{t}", [(ch[t], 1), (z[t], -ch_max[t])], upper=0)
            model.row(f"discharge_mutex_{name}_{t}", [(dis[t], 1), (z[t], dis_max[t])], upper=dis_max[t])
            if E == 0:
                model.equal(f"soc_dynamic_{name}_{t}", [(soc[t+1], 1), (soc[t], -1)])
                continue
            model.equal(f"soc_dynamic_{name}_{t}", [(soc[t+1], 1), (soc[t], -1),
                        (ch[t], -eta_ch[t] * dt / E), (dis[t], dt / (eta_dis[t] * E))])
            # Exact joint encoding of positive-part energy limits + mutex.
            # High-side idle chooses z=0; low-side idle chooses z=1.
            model.row(f"charge_positive_part_{name}_{t}",
                      [(ch[t], eta_ch[t] * dt), (soc[t], E), (z[t], E * (hi - band_hi[t]))], upper=E * hi)
            model.row(f"discharge_positive_part_{name}_{t}",
                      [(dis[t], dt / eta_dis[t]), (soc[t], -E), (z[t], -E * (band_lo[t] - lo))], upper=-E * band_lo[t])
        storage_data[name] = {"E": E, "P": P, "soc": soc, "ch": ch, "dis": dis, "z": z,
                              "lo": band_lo, "hi": band_hi, "hard_lo": lo, "hard_hi": hi,
                              "ch_max": ch_max, "dis_max": dis_max, "eta_ch": eta_ch, "eta_dis": eta_dis}
    area_limit = _num(site.get("area_limit_m2", system["storage_common"]["area_limit_m2"]), "area_limit_m2", True)
    model.row("storage_area_hard", {}, upper=area_limit - area)

    for t in range(T):
        model.equal(f"electric_balance_{t}", [(v["P_pv_used"][t], 1), (v["P_wind_used"][t], 1),
                    (v["g_imp"][t], 1), (storage_data["Na"]["dis"][t], 1), (storage_data["Li"]["dis"][t], 1),
                    (v["IT_served"][t], -pue[t]), (storage_data["Na"]["ch"][t], -1),
                    (storage_data["Li"]["ch"][t], -1), (v["P_hp"][t], -1), (v["g_exp"][t], -1)])
        model.equal(f"recoverable_heat_{t}", [(v["Q_rec"][t], 1), (v["IT_served"][t], -heat_factor[t])])
        model.equal(f"heat_source_balance_{t}", [(v["Q_rec"][t], 1), (v["P_hp"][t], -(cop - 1)), (v["Q_unused"][t], -1)])
        model.equal(f"heat_pump_output_{t}", [(v["Q_hp"][t], 1), (v["P_hp"][t], -cop)])
        model.equal(f"heat_output_balance_{t}", [(v["Q_hp"][t], 1), (v["Q_mun_hp"][t], -1),
                    (v["Q_ind"][t], -1), (v["Q_dump"][t], -1)])
        model.equal(f"municipal_delivery_{t}", [(v["Q_mun_hp"][t], 1), (v["Q_boiler"][t], 1),
                    (v["unserved_heat"][t], 1)], demand[t])
        model.row(f"municipal_min_upper_hp_{t}", [(v["Q_mun_hp"][t], 1), (v["Q_hp"][t], -1)], upper=0)
        model.row(f"municipal_min_lower_hp_{t}", [(v["Q_mun_hp"][t], 1), (v["Q_hp"][t], -1),
                  (v["y_heat_alloc"][t], M1[t])], lower=0)
        model.row(f"municipal_min_lower_demand_{t}", [(v["Q_mun_hp"][t], 1),
                  (v["y_heat_alloc"][t], -M2[t])], lower=demand[t] - M2[t])
        if mode == "normal":
            # Demand-side bound and economically positive industrial value imply
            # dump=0; fixing the known invariant eliminates numerical degeneracy.
            model.fix(v["Q_dump"][t], 0)
    if mode == "normal" and strategy == "S-carbon" and len(task_info):
        model.row("carbon_SLA_floor", late_terms, upper=(1 - exp["sla"]["sla_floor"]) * len(task_info))

    fault_policy = (_apply_fault_storage_policy(model, storage_data, econ, T, dt)
                    if mode == "fault" else None)
    n = len(model.variable_names)
    late = np.zeros(n)
    for j, coefficient in late_terms:
        late[j] += coefficient
    token_rate = _num(compute["token_per_gpu_hour"], "token_per_gpu_hour", True)
    task_tokens = sum(_num(x["row"].get("tokens_mtok", x["gpu"] * x["duration"] * token_rate), "tokens_mtok", True) for x in task_info)
    s1_tokens = float(base.sum() * dt * _num(compute["s1_equivalent_gpu_hours_per_mw"], "s1_equivalent_gpu_hours_per_mw", True) * token_rate)
    tokens = task_tokens + s1_tokens
    late_fraction = late / len(task_info) if task_info else late
    sla_penalty = late_fraction * econ["penalties"]["pen_sla"] * tokens
    thermal = system["thermal"]
    boiler_cost = _num(thermal["backup_heat"]["unit_cost_yuan_per_mwh_th"], "boiler_cost", True)
    price_ind = _num(thermal["heat_price_ind"], "heat_price_ind", True)
    price_mun = _num(thermal["heat_price_mun"], "heat_price_mun", True)
    external = econ["heat_settlement"]
    ind_price = price_ind if external["price_ind_is_external"] else 0
    mun_price = price_mun if external["price_mun_is_external"] else 0
    operating = model.vector([(v["g_imp"], price * dt), (v["g_exp"], -sell * dt),
                (v["Q_ind"], -ind_price * dt), (v["Q_mun_hp"], -mun_price * dt),
                (v["Q_boiler"], boiler_cost * dt), (v["P_curtail"], econ["penalties"]["pen_curtail"] * dt)])
    carbon_obj = model.vector([(v["g_imp"], carbon * dt), (v["Q_boiler"], econ["heat_carbon"]["ci_boiler"] * dt),
                              (v["Q_ind"], -econ["heat_carbon"]["ci_ind_substituted"] * dt)])
    heat_obj = model.vector([(v["Q_ind"], -ind_price * dt), (v["Q_mun_hp"], -mun_price * dt),
                            (v["Q_boiler"], boiler_cost * dt), (v["P_hp"], price * dt)]) + sla_penalty
    auxiliary_cfg = exp["auxiliary_lexicographic_term"]
    auxiliary = model.vector([(v["Q_dump"], auxiliary_cfg["eps_dump"] * dt),
                              (v["Q_unused"], auxiliary_cfg["eps_unused"] * dt)])
    components = {"operating_cost": operating, "sla_penalty": sla_penalty, "cost": operating + sla_penalty,
                  "carbon": carbon_obj, "heat": heat_obj, "auxiliary": auxiliary,
                  "late_fraction": late_fraction,
                  "critical": model.vector([(v["shed_c"], pue * dt)]),
                  "noncritical": model.vector([(v["shed_nc"], pue * dt)]),
                  "heat_gap": model.vector([(v["unserved_heat"], dt)])}
    if mode == "fault":
        preferred_later = fault_policy["discharge_order"][-1]
        components["chemistry_preference"] = model.vector([(storage_data[preferred_later]["dis"], dt)])
        primary = components["critical"]
    elif strategy == "S-balance":
        primary = sum(objective_settings["scales"][key] * components[key] for key in ("cost", "carbon", "heat"))
        primary += objective_settings["w_sla"] * late_fraction
        model.objective_constant = sum(objective_settings["offsets"].values())
    elif strategy == "S-base":
        primary = np.zeros(n)
    else:
        primary = components[{"S-cost": "cost", "S-carbon": "carbon", "S-heat": "heat"}[strategy]]
    components["primary"] = primary
    model.objective_components = components
    model.meta = {"ts": ts, "tasks": tasks, "system": system, "economics": econ, "experiments": exp,
                  "region": region, "chemistry": chemistry, "storage": storage_data, "variables": v,
                  "mode": mode, "strategy": strategy, "seed": int(seed), "T": T, "dt": dt,
                  "pue": pue, "temp_storage": temp_storage, "base": base, "pv": pv, "wind": wind,
                  "price": price, "sell": sell, "carbon": carbon, "demand": demand, "cop": cop,
                  "ind_capacity": ind_capacity, "import_capacity": import_capacity,
                  "heat_factor": heat_factor, "plan": plan, "p_card": p_card, "task_info": task_info,
                  "task_tokens": task_tokens, "s1_tokens_plan": s1_tokens, "tokens_plan": tokens,
                  "boiler_cost": boiler_cost, "ind_price": ind_price, "mun_price": mun_price,
                  "thermal_M": {"M1": M1, "M2": M2, "U": U}, "area_required_m2": area,
                  "area_limit_m2": area_limit, "objective_settings": objective_settings,
                  "fault_storage_policy": fault_policy}
    model.thermal_M = model.meta["thermal_M"]
    return model


def summarize_fault_service(traj, cfg, level1_proven=True):
    """Production threshold/scope helper; unproven island metrics are NaN."""
    econ = cfg.get("economics", cfg.get("econ", cfg))
    resilience = econ["resilience"]
    eps = _num(resilience["epsilon_shed_mw"], "epsilon_shed_mw", True)
    eps_heat = _num(resilience["epsilon_heat_mw_th"], "epsilon_heat_mw_th", True)
    critical = traj["shed_c"].to_numpy(dtype=float)
    heat = traj["unserved_heat"].to_numpy(dtype=float)
    if not np.isfinite(critical).all() or not np.isfinite(heat).all():
        raise ValueError("fault service trajectories must be finite")
    bad = np.flatnonzero(critical > eps)
    duration = len(traj) * float(traj.attrs.get("dt_hours", 1.0))
    full = bool(np.all(critical <= eps) and np.all(heat <= eps_heat))
    strict = bool(np.all(traj["it_shed"].to_numpy() == 0) and np.all(heat == 0))
    return {"t_island_h": (float(bad[0]) * float(traj.attrs.get("dt_hours", 1.0)) if len(bad) else duration) if level1_proven else float("nan"),
            "censored": bool(not len(bad)) if level1_proven else float("nan"),
            "full_service": full, "full_service_strict": strict, "level1_proven": bool(level1_proven),
            "epsilon_shed_mw": eps, "epsilon_heat_mw_th": eps_heat}


def _backend_info():
    result = {"backend": "scipy.optimize.milp/HiGHS", "scipy_version": scipy.__version__}
    try:
        from scipy.optimize._highspy import _core
        result["highs_version"] = ".".join(str(getattr(_core, "HIGHS_VERSION_" + name)) for name in ("MAJOR", "MINOR", "PATCH"))
    except (ImportError, AttributeError):
        result["highs_version"] = "bundled; version not exposed"
    return result


def _run_milp(model, objective, time_limit, gap):
    solver = model.meta["system"]["solver"]
    options = {"time_limit": max(float(time_limit), np.finfo(float).eps), "mip_rel_gap": float(gap),
               "presolve": bool(solver["presolve"]), "disp": bool(solver["verbose"]),
               "threads": int(solver["threads"]), "random_seed": int(solver["random_seed"])}
    if options["threads"] != 1:
        raise ValueError("SPEC requires solver.threads=1")
    with warnings.catch_warnings():
        # SciPy forwards these actual HiGHS options; this warning is about
        # SciPy's public list, not a fallback or an ignored solver setting.
        warnings.filterwarnings("ignore", message="Unrecognized options detected.*", category=RuntimeWarning)
        return milp(np.asarray(objective, dtype=float), integrality=np.asarray(model.integrality),
                    bounds=Bounds(np.asarray(model.lower), np.asarray(model.upper)),
                    constraints=model.constraint(), options=options)


def _stage_record(result, name, requested_gap):
    return {"level": name, "status": int(result.status), "message": str(result.message),
            "objective": float(result.fun) if result.fun is not None else float("nan"),
            "mip_gap": float(result.mip_gap) if getattr(result, "mip_gap", None) is not None else float("nan"),
            "mip_dual_bound": float(result.mip_dual_bound) if getattr(result, "mip_dual_bound", None) is not None else float("nan"),
            "requested_mip_gap": float(requested_gap),
            "proven_optimal": bool(result.status == 0 and getattr(result, "mip_gap", 0) == 0)}


def _extract_result(model, result, status, elapsed, stages, level1_proven=True):
    meta, x = model.meta, result.x
    v, storage = meta["variables"], meta["storage"]
    T, dt = meta["T"], meta["dt"]
    rules = "baseline_diagnostics" in meta
    backend = {"backend": "deterministic_fcfs_rules", "optimized": False} if rules else _backend_info()
    if x is None:
        info = {**backend, "time_s": elapsed, "level_statuses": stages,
                "message": str(result.message),
                "mip_gap": float(result.mip_gap) if getattr(result, "mip_gap", None) is not None else float("nan")}
        if rules:
            info.update(meta["baseline_diagnostics"])
            info["rule_diagnostics"] = meta["baseline_diagnostics"]
        return DispatchResult(status, float("nan"), pd.DataFrame(columns=TRAJECTORY_COLUMNS),
                              {}, info,
                              pd.DataFrame(columns=["task_id", "start_h", "end_h", "late"]))
    def values(name):
        return np.asarray(x[v[name]], float)
    data = {name: values(name) for name in v if name not in ("y_heat_alloc",)}
    data["y_heat_alloc"] = np.rint(values("y_heat_alloc")).astype(int)
    data["it_shed"] = data["shed_c"] + data["shed_nc"]
    data["unserved_elec"] = meta["pue"] * data["it_shed"]
    data["P_cool"] = (meta["pue"] - 1) * data["IT_load"]
    data["P_cool_served"] = (meta["pue"] - 1) * data["IT_served"]
    data["Q_low_used"] = data["P_hp"] * (meta["cop"] - 1)
    data["Q_mun_demand"] = meta["demand"]
    data["price_buy"] = meta["price"]
    data["price_sell"] = meta["sell"]
    data["T_amb"] = meta["ts"]["T_amb"].to_numpy()
    data["T_storage"] = meta["temp_storage"]
    data["PUE"] = meta["pue"]
    data["CI_grid"] = meta["carbon"]
    data["Q_ind_capacity"] = meta["ind_capacity"]
    soc_full = {}
    for name, st in storage.items():
        soc_full[name] = np.asarray(x[st["soc"]], float)
        data[f"soc_{name}"] = soc_full[name][:-1]
        data[f"p_ch_{name}"] = np.asarray(x[st["ch"]], float)
        data[f"p_dis_{name}"] = np.asarray(x[st["dis"]], float)
        data[f"soc_lo_{name}"], data[f"soc_hi_{name}"] = st["lo"], st["hi"]
        data[f"z_{name}"] = np.rint(x[st["z"]]).astype(int)
    econ = meta["economics"]
    gross = data["g_imp"] * meta["carbon"] * dt + data["Q_boiler"] * econ["heat_carbon"]["ci_boiler"] * dt
    avoid = data["Q_ind"] * econ["heat_carbon"]["ci_ind_substituted"] * dt
    data["carbon_net"] = gross - avoid
    traj = pd.DataFrame(data, index=meta["ts"].index)
    traj.attrs = {"mode": meta["mode"], "source": "仿真结果，非真实设备或站点数据",
                  "soc_terminal": {k: float(a[-1]) for k, a in soc_full.items()}, "dt_hours": dt,
                  "soc_initial": {k: float(a[0]) for k, a in soc_full.items()},
                  "sizing": {f"{key}_{name}": float(st[key]) for name, st in storage.items() for key in ("E", "P")},
                  "chemistry_scenarios": {name: chem.scenario for name, chem in meta["chemistry"].items()},
                  "level1_proven": bool(level1_proven), "region_id": meta["region"]["region_id"]}
    schedule = []
    for info in meta["task_info"]:
        values_start = np.asarray(x[info["indices"]])
        selected = int(np.argmax(values_start))
        start = int(info["starts"][selected])
        end = start + info["duration"]
        schedule.append({"task_id": info["row"]["task_id"], "start_h": start, "end_h": end,
                         "late": bool(end > info["row"]["deadline_h"])})
    task_schedule = pd.DataFrame(schedule, columns=["task_id", "start_h", "end_h", "late"])
    components = model.objective_components
    operating = float(components["operating_cost"] @ x)
    sla_penalty = float(components["sla_penalty"] @ x)
    delay_count = int(task_schedule["late"].sum()) if len(task_schedule) else 0
    sla_rate = 1 - delay_count / len(task_schedule) if len(task_schedule) else 1.0
    latencies = [row["end_h"] - info["row"]["arrive_h"] for row, info in zip(schedule, meta["task_info"], strict=True)]
    compute = {**meta["system"]["compute"], **meta["region"].get("compute_override", {})}
    s1_tokens = float(data["IT_served"].sum() * dt * compute["s1_equivalent_gpu_hours_per_mw"] * compute["token_per_gpu_hour"]) if meta["mode"] == "fault" else meta["s1_tokens_plan"]
    tokens = meta["task_tokens"] + s1_tokens
    carbon_total = float((gross - avoid).sum())
    if econ["carbon"]["include_manufacturing_in_net"]:
        annual_manufacturing = sum(storage[name]["E"] * meta["chemistry"][name].manufacturing_kgco2_per_mwh
                                   for name in storage) / econ["finance"]["project_life_years"]
        year_hours = float(meta["ts"].attrs.get("year_hours", 8760))
        carbon_total += annual_manufacturing * T * dt / year_hours
    renewable_used = float((data["P_pv_used"] + data["P_wind_used"]).sum() * dt)
    bus_demand = float((meta["pue"] * data["IT_served"] + data["P_hp"]
                       + data["p_ch_Na"] + data["p_ch_Li"]).sum() * dt)
    renewable_avail = float((meta["pv"] + meta["wind"]).sum() * dt)
    objective = float(components["primary"] @ x + model.objective_constant)
    if meta["strategy"] == "S-base" and meta["mode"] == "normal":
        objective = float("nan")  # rule-driven; operating_cost_yuan remains comparable.
    metrics = {"objective_value": objective, "operating_cost_yuan": operating,
               "cost_total": operating, "cost_grid": float((data["g_imp"] * meta["price"]).sum() * dt),
               "revenue_heat": float((data["Q_ind"] * meta["ind_price"] + data["Q_mun_hp"] * meta["mun_price"]).sum() * dt),
               "sla_penalty_yuan": sla_penalty, "sla_rate": sla_rate, "delay_count": delay_count,
               "latency_p50": float(np.median(latencies)) if latencies else 0.0,
               "latency_p90": float(np.percentile(latencies, 90)) if latencies else 0.0,
               "tokens_mtok": tokens, "task_tokens_mtok": meta["task_tokens"], "s1_tokens_mtok": s1_tokens,
               "cost_per_mtok": operating / tokens if tokens > 0 else float("nan"),
               "carbon_per_mtok": carbon_total / tokens if tokens > 0 else float("nan"),
               "carbon_grass": float(gross.sum()), "carbon_boiler": float((data["Q_boiler"] * econ["heat_carbon"]["ci_boiler"]).sum() * dt),
               "carbon_avoid": float(avoid.sum()), "carbon_net": carbon_total,
               "green_matching_rate": renewable_used / bus_demand if bus_demand > 0 else float("nan"),
               "curtailment_rate": float(data["P_curtail"].sum() * dt) / renewable_avail if renewable_avail > 0 else 0.0,
               "q_dump_total": float(data["Q_dump"].sum() * dt), "q_unused_total": float(data["Q_unused"].sum() * dt),
               "q_boiler_total": float(data["Q_boiler"].sum() * dt),
               "heat_from_waste_share": float(data["Q_mun_hp"].sum()) / float(meta["demand"].sum()) if meta["demand"].sum() > 0 else float("nan"),
               "unserved_elec": float(data["unserved_elec"].sum() * dt),
               "unserved_heat": float(data["unserved_heat"].sum() * dt),
               "eens_critical": float((meta["pue"] * data["shed_c"]).sum() * dt),
               "eens_heat": float(data["unserved_heat"].sum() * dt)}
    for name in storage:
        metrics[f"equivalent_cycles_{name}"] = float(np.maximum(0, -np.diff(soc_full[name])).sum())
    if meta["mode"] == "fault":
        metrics.update(summarize_fault_service(traj, {"economics": econ}, level1_proven))
    objective_coefficients = {key: {model.variable_names[i]: float(value) for i, value in enumerate(vector) if value}
                              for key, vector in components.items()}
    info = {**backend, "time_s": elapsed, "mip_gap": float(getattr(result, "mip_gap", np.nan)),
            "message": str(result.message), "n_variables": len(x), "n_constraints": len(model.rows),
            "variable_names": model.variable_names, "integrality": np.asarray(model.integrality),
            "objective_components": components, "objective_coefficients": objective_coefficients,
            "thermal_M": meta["thermal_M"], "level_statuses": stages, "level1_proven": bool(level1_proven),
            "soc_full": soc_full, "objective_unit": meta["objective_settings"]["unit"],
            "primary_objective_value": float("nan") if rules else float(components["primary"] @ x + model.objective_constant),
            "auxiliary_objective_value": float(components["auxiliary"] @ x),
            "backend_objective_value": float("nan") if rules else float(result.fun),
            "auxiliary_method": "not_applicable_rules" if rules else meta["experiments"]["auxiliary_lexicographic_term"]["method"],
            "objective_constant": model.objective_constant,
            "positive_part_encoding": "exact joint feasible region with charge/discharge mutex z",
            "simulation_notice": "仿真结果，非真实设备或站点数据",
            "green_matching_scope": "direct renewable input / served bus consumption including charging; storage origins not traced"}
    if meta["mode"] == "fault":
        info["fault_storage_policy"] = meta["fault_storage_policy"]
        info["fault_service_freeze"] = meta.get("fault_service_freeze")
    if rules:
        info.update(meta["baseline_diagnostics"])
        info["rule_diagnostics"] = meta["baseline_diagnostics"]
    if "normal_incumbent_cut" in meta:
        info["normal_incumbent_cut"] = meta["normal_incumbent_cut"]
    if "normal_bound_certificate" in meta:
        info["normal_bound_certificate"] = meta["normal_bound_certificate"]
    if meta["strategy"] == "S-balance" and meta["mode"] == "normal":
        # v2.10 §3.5 归一化越界守卫：任一分量归一化后 ∉ [0,1] → 告警并
        # **原样报告**归一化值与原始值；不得静默截断——截断会把"区间标定
        # 过窄"（标定缺陷）伪装成"策略触及边界"（正常结果），报告里无法区分。
        # 守卫是报告层：目标函数本身是归一化空间上的线性式，求解不受影响。
        ranges = meta["experiments"]["normalization"]["ranges"]
        guard = {}
        for comp in ("cost", "carbon", "heat"):
            raw_value = float(components[comp] @ x)
            low = _num(ranges[comp]["min"], f"normalization.{comp}.min")
            high = _num(ranges[comp]["max"], f"normalization.{comp}.max")
            normalized = (raw_value - low) / (high - low)
            outside = not (-1e-9 <= normalized <= 1.0 + 1e-9)
            guard[comp] = {"raw": raw_value, "normalized": normalized,
                           "range_min": low, "range_max": high,
                           "outside_unit_interval": outside}
            if outside:
                warnings.warn(
                    f"S-balance normalization guard: {comp} normalized={normalized:.6f} "
                    f"outside [0,1] (raw={raw_value:.6f}, range=[{low}, {high}]); "
                    "reported as-is, NOT clipped (SPEC v2.10 §3.5). 区间可能需要重新标定；"
                    "重新标定必须整批重跑", RuntimeWarning, stacklevel=2)
        guard["fired"] = any(guard[c]["outside_unit_interval"] for c in ("cost", "carbon", "heat"))
        info["s_balance_normalization_guard"] = guard
        metrics["s_balance_guard_fired"] = guard["fired"]
    return DispatchResult(status, objective, traj, metrics, info, task_schedule)


def solve_dispatch(ts, tasks, cfg, sizing, strategy, seed,
                   time_limit_s=60.0, mip_gap=0.01, mode="normal"):
    """Solve the named integer model with actual HiGHS, never an LP surrogate.

    The fault first level requests gap=0 and must have a proof before subsequent
    levels can fix its optimum. A time-limited incumbent remains inspectable as
    feasible_not_proven; its island/censoring metrics are NaN.
    """
    started = perf_counter()
    model = build_dispatch_model(ts, tasks, cfg, sizing, strategy, seed, mode)
    model.thermal_M = model.meta["thermal_M"]
    if time_limit_s <= 0 or not 0 <= mip_gap < 1:
        raise ValueError("requires positive time limit and mip_gap in [0,1)")
    stages = []
    def run(objective, gap, label):
        result = _run_milp(model, objective, time_limit_s - (perf_counter() - started), gap)
        stages.append(_stage_record(result, label, gap))
        return result
    components = model.objective_components
    aux = model.meta["experiments"]["auxiliary_lexicographic_term"]
    if mode == "fault":
        result = run(components["critical"], 0.0, "critical")
        if result.x is None:
            status = {2: "infeasible", 3: "unbounded"}.get(result.status, "error")
            return _extract_result(model, result, status, perf_counter() - started, stages, False)
        proven = stages[-1]["proven_optimal"]
        if not proven:
            return _extract_result(model, result, "feasible_not_proven", perf_counter() - started, stages, False)
        first_value = float(components["critical"] @ result.x)
        model.row("fix_critical_optimum", enumerate(components["critical"]), upper=first_value)
        service_levels_complete = True
        for key in ("noncritical", "heat_gap"):
            candidate = run(components[key], mip_gap, key)
            if candidate.x is None:
                service_levels_complete = False
                break
            result = candidate
            optimum = float(components[key] @ result.x)
            allowance = max(aux["tau_floor"], aux["tau_rel"] * abs(optimum))
            model.row(f"fix_{key}_within_tolerance", enumerate(components[key]), upper=optimum + allowance)
        if service_levels_complete:
            # Keep the configured service-stage tolerance rows. The preference
            # stage is additionally restricted to the service actually attained
            # by stage three; it cannot spend those tolerances on less Li use.
            # These are achieved values, not claims of stage-2/3 global proof.
            frozen_values, frozen_rows = {}, {}
            for key in ("critical", "noncritical", "heat_gap"):
                value = float(components[key] @ result.x)
                row_name = f"freeze_service_before_preference_{key}"
                model.equal(row_name, enumerate(components[key]), value)
                frozen_values[key], frozen_rows[key] = value, row_name
            model.meta["fault_service_freeze"] = {"values": frozen_values, "rows": frozen_rows,
                "basis": "achieved feasible stage-three service; not a global optimality claim"}
            candidate = run(components["chemistry_preference"], mip_gap, "chemistry_preference")
            if candidate.x is not None:
                result = candidate
        status = "optimal" if all(stage["status"] == 0 for stage in stages) else "feasible"
        return _extract_result(model, result, status, perf_counter() - started, stages, True)
    if strategy == "S-base":
        outcome = _apply_baseline_rules(model)
        model.meta["baseline_diagnostics"] = outcome.diagnostics
        return _extract_result(model, outcome, outcome.status, perf_counter() - started, [])
    elif aux["method"] == "weighted_single_solve":
        objective = components["primary"] + components["auxiliary"]
        # BT2-①（v2.10/M4）：S-base 规则解 + c·x ≤ c·x₀ 割的热启动对**全部
        # 优化策略**生效。割对任意线性目标都成立（可行 x₀ ⇒ 最优 x* 满足
        # c·x* ≤ c·x₀），不会切掉全局最优；候选排程必须先通过原模型的
        # 逐约束独立验证（S-carbon 的 sla_floor 等策略专属行同样被检查，
        # 不满足即整体拒绝并回退原求解路径）。此前仅 S-cost 启用，导致
        # 其余策略在 168h/2200 任务规模下 60s 内找不到首个可行解而返回
        # error（HiGHS Status 13, primal None）。
        if strategy != "S-base" and len(model.meta["task_info"]) >= 500:
            from aidc.model._incumbent import add_feasible_cost_cut
            if strategy == "S-cost":
                search_deadline = started + time_limit_s - 3.   # M2.5 审计过的原时序
            else:
                # 非 S-cost 没有认证下界分支：搜索至多占用一半预算，其余留给
                # 主 MILP——带上 c·x ≤ c·x₀ 割后，主求解只需搜索"优于种子"的
                # 空间，既可能找到更优解，也可能据此证明种子最优。
                search_deadline = started + max(10.0, time_limit_s * 0.5)
            model.meta["normal_incumbent_cut"] = add_feasible_cost_cut(
                model, objective, deadline=search_deadline, run_milp=_run_milp)
        seed_x = model.meta.get("_normal_feasible_incumbent")
        lower_bound = float("nan")
        if seed_x is not None:
            from aidc.model._incumbent import _merge_lower_bounds
        if seed_x is not None and strategy == "S-cost":
            # 成本下界认证模型只在 S-cost 下经过完整审计（M2.5），其余策略
            # 使用"割 + 验证过的保留可行解"路径，不做认证宣称。
            from aidc.model._cost_bound import solve_cost_lower_bound
            remaining = max(0., time_limit_s - (perf_counter() - started) - 3.)
            lower_bound, bound_audit = solve_cost_lower_bound(model, objective, remaining, .001, _run_milp)
            upper_bound = float(objective @ seed_x + model.objective_constant)
            valid_bound = np.isfinite(lower_bound) and lower_bound <= upper_bound + 1e-6
            if not valid_bound:
                lower_bound = float("nan")
            certified_gap = max(0., (upper_bound - lower_bound) / max(abs(upper_bound), 1e-10)) if valid_bound else float("nan")
            model.meta["normal_bound_certificate"] = {
                "method": "original integer feasible schedule plus valid global lower bound",
                "upper_bound": upper_bound, "lower_bound": lower_bound if valid_bound else float("nan"),
                "global_mip_gap": certified_gap, "target_gap": mip_gap,
                "target_met": bool(valid_bound and certified_gap <= mip_gap), "bound_audit": bound_audit,
                "relaxed_trajectory_returned": False}
            if valid_bound and certified_gap <= mip_gap:
                result = OptimizeResult(x=seed_x.copy(), fun=float(objective @ seed_x), status=1,
                    message="original-model integer feasible incumbent certified within requested global gap",
                    mip_gap=certified_gap, mip_dual_bound=lower_bound-model.objective_constant)
                stages.append(_stage_record(result, "normal_certified_feasible", mip_gap))
            elif time_limit_s - (perf_counter() - started) <= 3.:
                result = OptimizeResult(x=seed_x.copy(), fun=float(objective @ seed_x), status=1,
                    message="time budget exhausted; retained verified integer incumbent and available bound",
                    mip_gap=certified_gap, mip_dual_bound=lower_bound-model.objective_constant)
                stages.append(_stage_record(result, "normal_verified_incumbent", mip_gap))
            else:
                result = run(objective, mip_gap, "normal_weighted")
        else:
            result = run(objective, mip_gap, "normal_weighted")
        if seed_x is not None:
            if result.x is None or float(objective @ seed_x) < float(result.fun):
                # Retain a validated upper bound if the backend finds none.
                result.x, result.fun, result.status = seed_x.copy(), float(objective @ seed_x), 1
                result.message = str(result.message) + "; retained independently verified feasible incumbent"
            full_dual = getattr(result, "mip_dual_bound", float("nan"))
            upper = result.fun + model.objective_constant
            best_bound, result.mip_gap = _merge_lower_bounds(upper, (
                lower_bound, full_dual + model.objective_constant if full_dual is not None else None))
            result.mip_dual_bound = best_bound-model.objective_constant
            if "normal_bound_certificate" in model.meta:
                model.meta["normal_bound_certificate"].update(
                    upper_bound=upper, lower_bound=best_bound, global_mip_gap=result.mip_gap,
                    target_met=bool(np.isfinite(result.mip_gap) and result.mip_gap <= mip_gap))
    elif aux["method"] == "two_stage":
        result = run(components["primary"], mip_gap, "normal_primary")
        if result.x is not None:
            optimum = float(components["primary"] @ result.x + model.objective_constant)
            allowance = max(aux["tau_floor"], aux["tau_rel"] * abs(optimum))
            model.row("normal_primary_within_tolerance", enumerate(components["primary"]),
                      upper=optimum + allowance - model.objective_constant)
            candidate = run(components["auxiliary"], mip_gap, "normal_auxiliary")
            if candidate.x is not None:
                result = candidate
    else:
        raise ValueError("unknown auxiliary_lexicographic_term.method")
    status = ("optimal" if result.status == 0 else "feasible") if result.x is not None else {2: "infeasible", 3: "unbounded"}.get(result.status, "error")
    return _extract_result(model, result, status, perf_counter() - started, stages)


def _apply_fault_storage_policy(model, storage, economics, horizon, dt):
    """Encode fault allocation without forbidding same-hour dual discharge.

    Sequential preference is an objective only, applied after all three service
    levels. Parallel allocation uses temperature-dependent *power* caps as fixed
    weights. A common utilisation lambda allocates p_x=min(cap_x*lambda,
    positive SOC-energy limit_x). Thus an energy-limited chemistry saturates
    while the other continues; a hard ratio cannot create artificial shedding.
    All min/positive-part relations below are exact mixed-integer linear rows.
    """
    resilience = economics["resilience"]
    order = list(resilience["discharge_order"])
    if len(order) != 2 or set(order) != {"Na", "Li"}:
        raise ValueError("discharge_order must be a permutation of ['Na', 'Li']")
    parallel = resilience["parallel_discharge"]
    if not isinstance(parallel, (bool, np.bool_)):
        raise ValueError("parallel_discharge must be boolean")
    policy = {"discharge_order": order, "parallel_discharge": bool(parallel),
              "same_hour_dual_discharge_allowed": True,
              "preference_level": 4, "preference_chemistry": order[-1],
              "ratio_basis": "temperature power caps, excluding SOC energy limits",
              "encoding": "service-first lexicographic preference"}
    if not parallel:
        return policy

    ratio_start = len(model.row_names)
    utilisation = model.variable("fault_parallel_utilisation", horizon, upper=1.0)
    for name, st in storage.items():
        # The positive energy limit may exceed the power cap; retaining that
        # distinction is necessary for the exact bounded-ratio min relation.
        rate = st["eta_dis"] * st["E"] / dt
        maximum = rate * np.maximum(0., st["hard_hi"] - st["lo"])
        negative = rate * np.maximum(0., st["lo"] - st["hard_lo"])
        energy_limit = model.variable(f"fault_energy_limit_{name}", horizon, upper=maximum)
        positive = model.variable(f"fault_energy_positive_{name}", horizon, upper=1., integer=True)
        saturated = model.variable(f"fault_parallel_saturated_{name}", horizon, upper=1., integer=True)
        for t in range(horizon):
            q, b, s, p, soc = energy_limit[t], positive[t], saturated[t], st["dis"][t], st["soc"][t]
            k, hi, low, cap = rate[t], maximum[t], negative[t], st["dis_max"][t]
            model.row(f"fault_energy_positive_lower_{name}_{t}",
                      [(q, 1), (soc, -k)], lower=-k * st["lo"][t])
            model.row(f"fault_energy_positive_upper_state_{name}_{t}",
                      [(q, 1), (soc, -k), (b, low)], upper=low - k * st["lo"][t])
            model.row(f"fault_energy_positive_upper_zero_{name}_{t}", [(q, 1), (b, -hi)], upper=0)
            model.row(f"fault_parallel_upper_energy_{name}_{t}", [(p, 1), (q, -1)], upper=0)
            model.row(f"fault_parallel_upper_share_{name}_{t}", [(p, 1), (utilisation[t], -cap)], upper=0)
            model.row(f"fault_parallel_lower_energy_{name}_{t}", [(p, 1), (q, -1), (s, -hi)], lower=-hi)
            model.row(f"fault_parallel_lower_share_{name}_{t}", [(p, 1), (utilisation[t], -cap), (s, cap)], lower=0)
    policy["encoding"] = "bounded proportional allocation: p_x=min(power_cap_x*lambda,positive_SOC_energy_limit_x)"
    policy["constraint_rows"] = model.row_names[ratio_start:]
    return policy


def _apply_baseline_rules(model):
    from aidc.model._baseline import build_baseline_outcome
    return build_baseline_outcome(model)


def verify_power_balance(trajectories, tol=1e-6, mode="normal"):
    """Check the same served-load bus equation in both modes."""
    if mode not in ("normal", "fault") or not np.isfinite(tol) or tol < 0:
        raise ValueError("invalid mode or power-balance tolerance")
    t = trajectories
    lhs = t.P_pv_used + t.P_wind_used + t.g_imp + t.p_dis_Na + t.p_dis_Li
    rhs = t.IT_served + t.P_cool_served + t.p_ch_Na + t.p_ch_Li + t.P_hp + t.g_exp
    residual = float(np.max(np.abs(np.asarray(lhs - rhs)), initial=0))
    return residual < tol, residual


def verify_heat_balance(trajectories, tol=1e-6):
    """Check source, HP output and municipal delivery including recorded gaps."""
    if not np.isfinite(tol) or tol < 0:
        raise ValueError("heat-balance tolerance must be finite and nonnegative")
    t = trajectories
    residuals = [t.Q_hp - t.Q_mun_hp - t.Q_ind - t.Q_dump,
                 t.Q_mun_hp + t.Q_boiler + t.unserved_heat - t.Q_mun_demand,
                 t.Q_rec - t.Q_low_used - t.Q_unused]
    residual = max(float(np.max(np.abs(np.asarray(x)), initial=0)) for x in residuals)
    return residual <= tol, residual

