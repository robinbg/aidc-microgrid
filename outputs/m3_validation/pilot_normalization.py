"""M3 试点标定：S-balance 内层归一化范围（experiments.yaml normalization.ranges）。

依据：
  - experiments.yaml `normalization`：source = pilot_experiment，
    pilot_seed = 20260816，pilot_n_samples = 50，"null 表示尚未标定，
    M3 阶段跑完试点实验后回填"。
  - SPEC §3.5 / §4.3：S-balance 三项（cost / carbon / heat）需按试点
    min-max 范围线性归一化后加权；范围一旦固定，所有实验复用。

口径（严格按 dispatch.py 的 components 定义从轨迹重构，不用近似）：
    cost   = operating_cost_yuan + sla_penalty_yuan          （components["cost"]）
    carbon = Σ_t [g_imp·CI_grid + Q_boiler·ci_boiler − Q_ind·ci_ind_sub]·Δt
             （components["carbon"]，kgCO₂，不含制造碳——制造碳只进外层 f2 年报）
    heat   = Σ_t [−Q_ind·price_ind·1_ext − Q_mun_hp·price_mun·1_ext
                  + Q_boiler·boiler_cost + P_hp·price_buy]·Δt + sla_penalty_yuan
             （components["heat"]）

采样：default_rng(pilot_seed) 在 sizing_bounds 上均匀采样；面积超限
（E_Na·area_Na + E_Li·area_Li > area_limit）在求解前预筛除并计数
（§4.3：不可行点跳过，不进入训练/范围）；求解期不可行同样跳过。
范围 = 全部可行 (样本 × 代表周) 值的 min/max（R-NW 画像、S-cost 策略、
低保真 5 代表周；扩到四画像与否属规格方决定，本脚本如实记录口径）。

⚠️ 本脚本**不修改** configs/experiments.yaml；产出建议回填片段，
   由规格方决定是否采纳（YAML 哈希冻结是 M1 起的验收不变量）。

用法：
    python -B outputs/m3_validation/pilot_normalization.py [workers=5]
"""
from __future__ import annotations

import json
import multiprocessing as mp
import sys
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

SEED = 20260816          # experiments.yaml normalization.pilot_seed
N_SAMPLES = 50           # experiments.yaml normalization.pilot_n_samples
REGION = "R-NW"
STRATEGY = "S-cost"
TIME_LIMIT_S = 60.0
MIP_GAP = 0.01

_G: dict = {}


def _init_worker() -> None:
    from aidc.data.chemistry import load_config_bundle
    from aidc.data.tasks import generate_tasks
    from aidc.data.timeseries import build_year_timeseries, select_representative_weeks
    cfg = load_config_bundle(ROOT / "configs")
    cfg["region"] = deepcopy(cfg["regions"][REGION])
    ts_year = build_year_timeseries(cfg["region"], cfg["system"], cfg["economics"],
                                    cfg["generation"], SEED)
    weeks, weights, week_ids = select_representative_weeks(
        ts_year, cfg["experiments"]["fidelity"]["low"])
    tasks_cfg = cfg["experiments"]["tasks"]
    tol = float(tasks_cfg["s3_tolerance_hours_by_region"].get(
        REGION, tasks_cfg["s3_tolerance_hours_default"]))
    ratio = float(cfg["region"]["compute_override"]["flexible_task_ratio"])
    tasks = [generate_tasks(float(w["IT_base"].sum()), len(w),
                            int(w.attrs["gpu_capacity_plan"]), ratio, SEED,
                            float(w.attrs["gpu_power_mw_per_card"]), tol,
                            cfg["generation"]) for w in weeks]
    _G.update(cfg=cfg, weeks=weeks, weights=weights, week_ids=week_ids, tasks=tasks)


def _components(traj: pd.DataFrame, metrics: dict, cfg: dict) -> dict:
    econ, thermal = cfg["economics"], cfg["system"]["thermal"]
    hs = econ["heat_settlement"]
    ind_price = float(thermal["heat_price_ind"]) if hs["price_ind_is_external"] else 0.0
    mun_price = float(thermal["heat_price_mun"]) if hs["price_mun_is_external"] else 0.0
    boiler = float(thermal["backup_heat"]["unit_cost_yuan_per_mwh_th"])
    hc = econ["heat_carbon"]
    ci_boiler, ci_sub = float(hc["ci_boiler"]), float(hc["ci_ind_substituted"])
    dt = 1.0
    sla = float(metrics["sla_penalty_yuan"])
    carbon = float((traj["g_imp"] * traj["CI_grid"] + traj["Q_boiler"] * ci_boiler
                    - traj["Q_ind"] * ci_sub).sum() * dt)
    heat = float((-traj["Q_ind"] * ind_price - traj["Q_mun_hp"] * mun_price
                  + traj["Q_boiler"] * boiler + traj["P_hp"] * traj["price_buy"]).sum() * dt) + sla
    return {"cost": float(metrics["operating_cost_yuan"]) + sla, "carbon": carbon, "heat": heat}


def _run_sample(payload: tuple[int, dict]) -> dict:
    from aidc.model.dispatch import solve_dispatch
    idx, sizing = payload
    cfg, weeks, weights, week_ids, tasks = (_G["cfg"], _G["weeks"], _G["weights"],
                                            _G["week_ids"], _G["tasks"])
    started = time.perf_counter()
    rows = []
    status = "feasible"
    for week, weight, week_id, task in zip(weeks, weights, week_ids, tasks, strict=True):
        result = solve_dispatch(week, task, cfg, sizing, STRATEGY, SEED,
                                time_limit_s=TIME_LIMIT_S, mip_gap=MIP_GAP, mode="normal")
        if result.status == "infeasible" or result.trajectories.empty:
            status = "infeasible"
            rows.append({"week_id": week_id, "status": result.status})
            break
        values = _components(result.trajectories, result.metrics, cfg)
        info = result.solver_info
        gap = info.get("mip_gap")
        rows.append({"week_id": week_id, "weight": float(weight), "status": result.status,
                     "mip_gap": float(gap) if gap is not None and np.isfinite(gap) else None,
                     "certificate_target_met": (info.get("normal_bound_certificate") or {}).get("target_met"),
                     "time_s": info.get("time_s"), **values})
    return {"idx": idx, "sizing": sizing, "status": status,
            "elapsed_s": time.perf_counter() - started, "weeks": rows}


def _prefilter_ok(sizing: dict, cfg: dict) -> bool:
    from aidc.data.chemistry import chemistry_params_from_config
    na = chemistry_params_from_config(cfg["chemistry_na"])
    li = chemistry_params_from_config(cfg["chemistry_li"])
    area = sizing["E_Na"] * na.area_per_mwh + sizing["E_Li"] * li.area_per_mwh
    site = {**cfg["system"]["storage_common"], **cfg["region"].get("storage_site_override", {})}
    return area <= float(site["area_limit_m2"])


def main() -> None:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    OUT.mkdir(parents=True, exist_ok=True)
    from aidc.data.chemistry import load_config_bundle
    cfg = load_config_bundle(ROOT / "configs")
    cfg["region"] = deepcopy(cfg["regions"][REGION])
    bounds = cfg["experiments"]["sizing_bounds"]
    rng = np.random.default_rng(SEED)
    samples, draws, n_prefiltered = [], 0, 0
    while len(samples) < N_SAMPLES:
        draws += 1
        sizing = {k: float(rng.uniform(v[0], v[1])) for k, v in bounds.items()}
        if not _prefilter_ok(sizing, cfg):
            n_prefiltered += 1
            continue
        samples.append((len(samples), sizing))
    jsonl = OUT / "pilot_samples.jsonl"
    jsonl.write_text("", encoding="utf-8")
    print(f"pilot: {N_SAMPLES} feasible draws (+{n_prefiltered} area-prefiltered), "
          f"{workers} workers", flush=True)
    started = time.perf_counter()
    done = 0
    with mp.Pool(workers, initializer=_init_worker) as pool:
        for record in pool.imap_unordered(_run_sample, samples, chunksize=1):
            done += 1
            with jsonl.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            if done % 5 == 0 or done == len(samples):
                print(f"  {done}/{len(samples)} elapsed={time.perf_counter()-started:.0f}s", flush=True)

    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    pooled = {"cost": [], "carbon": [], "heat": []}
    n_infeasible, gaps, uncertified = 0, [], 0
    for rec in records:
        if rec["status"] != "feasible":
            n_infeasible += 1
            continue
        for row in rec["weeks"]:
            if row.get("status") != "infeasible":
                for key in pooled:
                    pooled[key].append(row[key])
                if row.get("mip_gap") is not None:
                    gaps.append(row["mip_gap"])
                if row.get("certificate_target_met") is False:
                    uncertified += 1
    ranges = {k: {"min": float(min(v)), "max": float(max(v))} for k, v in pooled.items()}
    for key, bounds_ in ranges.items():
        assert bounds_["max"] > bounds_["min"], f"{key}: degenerate range"
    proposal = ("normalization:\n  source: \"pilot_experiment\"\n"
                f"  pilot_seed: {SEED}\n  pilot_n_samples: {N_SAMPLES}\n  ranges:\n"
                + "".join(f"    {k}: {{min: {v['min']:.6f}, max: {v['max']:.6f}}}\n"
                          for k, v in ranges.items()))
    report = {
        "stage": "M3-normalization-pilot", "simulation": True,
        "purpose": "S-balance 内层三项归一化范围（experiments.yaml normalization.ranges 的建议回填值）",
        "method": {
            "region": REGION, "strategy_for_pilot": STRATEGY,
            "fidelity": "low (5 representative weeks, 168h each)",
            "seed": SEED, "n_draws": draws, "n_area_prefiltered": n_prefiltered,
            "n_samples_solved": len(records), "n_samples_infeasible": n_infeasible,
            "n_week_values": {k: len(v) for k, v in pooled.items()},
            "component_formulas": "cost = operating + SLA penalty; carbon = grid+boiler−ind_sub (kgCO2, excl. manufacturing); heat = −ind_rev−mun_rev·1_ext+boiler+hp_elec+SLA (exact §3.4 components)",
            "time_limit_s": TIME_LIMIT_S, "mip_gap_target": MIP_GAP,
            "windows_not_certified_within_target": uncertified,
            "gap_distribution": {"min": min(gaps) if gaps else None,
                                 "median": float(np.median(gaps)) if gaps else None,
                                 "max": max(gaps) if gaps else None},
            "note": "范围基于 R-NW 单画像试点；是否需四画像联合标定属规格方决定",
        },
        "ranges": ranges,
        "proposed_yaml_snippet": proposal,
        "elapsed_s": time.perf_counter() - started,
        "yaml_not_modified": True,
    }
    (OUT / "normalization_pilot.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"ranges": ranges, "n_week_values": report["method"]["n_week_values"],
                      "n_infeasible": n_infeasible, "uncertified": uncertified,
                      "elapsed_s": report["elapsed_s"]}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
