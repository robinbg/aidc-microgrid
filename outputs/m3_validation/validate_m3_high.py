"""M3 高保真闭环验证：滚动年度（52 窗 SOC 传递）+ 年度故障场景 → f3。

用法：
    python -B outputs/m3_validation/validate_m3_high.py [A3|A0|A1|A2]

验证内容（全部独立手算，不复读实现）：
  1. 年度总量 = 逐窗总量直接求和（无权重外推）；
  2. LCOE / f2 / f4 用 §4.3 公式从年度总量重算；
  3. f3 用 §5.4 唯一公式从韧性聚合值重算（参考值取配置常数 50 MWh / 12 h）；
  4. SOC 衔接审计：windows[k].soc_initial == windows[k-1].soc_terminal（1e-9），
     首窗初值 = soc_init，末窗终值 = soc_init（§13.1）；
  5. 删失/未证明比例如实上报（不得把下界均值当真实续航）；
  6. 11 份 YAML 哈希与 M1 冻结基线一致；_baseline.py 与 M2.9 冻结一致。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from aidc.data.chemistry import chemistry_params_from_config, load_config_bundle  # noqa: E402
from aidc.sizing.objective import evaluate_sizing  # noqa: E402
from aidc.data.tasks import TASK_COLUMNS  # noqa: E402

SEED = 20260816
REGION = "R-NW"
STRATEGY = "S-cost"


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, pd.DataFrame):
        return clean(value.to_dict(orient="records"))
    if isinstance(value, (np.ndarray, list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def write(path: Path, value) -> None:
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def config_hashes() -> dict:
    paths = sorted((ROOT / "configs").rglob("*.yaml"))
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths}


def main(arm_id: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    bundle = load_config_bundle(ROOT / "configs")
    sizing = deepcopy(next(a["sizing_fixed"] for a in bundle["experiments"]["arms"]
                           if a["id"] == arm_id))
    frozen = {k.replace("\\", "/"): v for k, v in json.loads(
        (ROOT / "outputs/m1_validation/protected_baseline.json")
        .read_text(encoding="utf-8")).items()}
    frozen_m29 = json.loads((ROOT / "outputs/m2_v29_validation/final_summary.json")
                            .read_text(encoding="utf-8"))["frozen_expected_sha256"]
    hashes_before = config_hashes()
    baseline_hash = hashlib.sha256(
        (ROOT / "src/aidc/model/_baseline.py").read_bytes()).hexdigest()

    print(f"START high-fidelity closed loop: arm={arm_id} sizing={sizing} "
          f"strategy={STRATEGY} region={REGION} seed={SEED}", flush=True)
    started = time.perf_counter()
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    result = evaluate_sizing(deepcopy(sizing), REGION, empty, "high", STRATEGY, SEED,
                             ROOT / "configs")
    elapsed = time.perf_counter() - started
    print(f"SOLVE DONE: status={result['status']} elapsed={elapsed:.1f}s", flush=True)
    assert result["status"] in ("feasible", "optimal"), result["status"]

    windows = result["windows"]
    solved = [w for w in windows if w["status"] not in ("skipped",)]
    # ---- 1) 年度总量 = 逐窗求和（手算）
    totals_keys = list(solved[0]["totals"])
    annual = {k: float(sum(w["totals"][k] for w in solved)) for k in totals_keys
              if k not in ("latency_p50_h", "latency_p90_h")}
    checks: dict = {}

    def close(name, hand, reported, rtol=1e-9):
        checks[name] = {"hand": hand, "reported": reported,
                        "ok": bool(np.isclose(hand, reported, rtol=rtol))}

    close("annual_energy_served_mwh", annual["energy_served_mwh"],
          result["metrics"]["annual_energy_served_mwh"])
    close("annual_operating_cost", annual["operating_cost_yuan"], result["metrics"]["cost_total"])
    close("annual_carbon_net_kg", annual["carbon_net_kg"], result["metrics"]["carbon_net"])
    close("annual_tokens_mtok", annual["tokens_mtok"], result["metrics"]["tokens_mtok"])

    # ---- 2) 目标手算
    finance = bundle["economics"]["finance"]
    rate, years = float(finance["discount_rate"]), float(finance["project_life_years"])
    crf = rate * (1 + rate) ** years / ((1 + rate) ** years - 1)
    storage = 0.0
    for name in ("Na", "Li"):
        chem = chemistry_params_from_config(bundle[f"chemistry_{name.lower()}"])
        E, P = sizing[f"E_{name}"], sizing[f"P_{name}"]
        c = annual[f"equivalent_cycles_{name}"]
        life = min(chem.calendar_life, chem.cycle_life / c if c > 0 else float("inf"))
        storage += (crf * (E * chem.unit_cost_energy + P * chem.unit_cost_power)
                    + E * chem.opex_annual
                    + E * chem.unit_cost_energy * max(0.0, 1.0 / life - 1.0 / years))
    close("storage_annualized_cost", storage, result["storage_annualized_cost"])
    served = annual["energy_served_mwh"]
    close("LCOE_f1", (annual["operating_cost_yuan"] + storage) / served, result["LCOE"])
    close("carbon_per_mtok_f2", annual["carbon_net_kg"] / annual["tokens_mtok"],
          result["carbon_per_mtok"])
    close("sla_rate_f4", 1.0 - annual["delay_count"] / annual["n_tasks"], result["sla_rate"])

    # ---- 3) f3 手算（§5.4 唯一公式，参考值取配置常数）
    res_cfg = bundle["economics"]["resilience"]
    agg = result["resilience"]["aggregate"]
    w_e, w_i = float(res_cfg["w_eens"]), float(res_cfg["w_island"])
    e_ref, t_ref = float(res_cfg["eens_reference_mwh"]), float(res_cfg["t_island_reference_hours"])
    f3_hand = w_e * (agg["eens"] / e_ref) + w_i * max(0.0, 1.0 - agg["t_island_mean"] / t_ref)
    close("resilience_gap_f3", f3_hand, result["resilience_gap"])

    # ---- 4) SOC 衔接审计（§13.1）
    na = chemistry_params_from_config(bundle["chemistry_na"])
    li = chemistry_params_from_config(bundle["chemistry_li"])
    soc_init = {"Na": float(na.soc_init), "Li": float(li.soc_init)}
    continuity_errors = []
    for k in range(1, len(solved)):
        for name in ("Na", "Li"):
            prev_end = solved[k - 1]["soc_terminal"][name]
            this_start = solved[k]["soc_initial"][name]
            if abs(prev_end - this_start) > 1e-9:
                continuity_errors.append((k, name, prev_end, this_start))
    first_ok = all(abs(solved[0]["soc_initial"][n] - soc_init[n]) < 1e-12 for n in ("Na", "Li"))
    last_ok = all(abs(solved[-1]["soc_terminal"][n] - soc_init[n]) < 1e-9 for n in ("Na", "Li"))
    checks["soc_carry_over"] = {"continuity_violations": continuity_errors,
                                "first_window_at_soc_init": first_ok,
                                "last_window_returns_to_soc_init": last_ok,
                                "ok": not continuity_errors and first_ok and last_ok}
    checks["windows_complete"] = {"n_windows": len(windows), "n_solved": len(solved),
                                  "ok": len(solved) == len(windows) == 52}

    # ---- 5) 韧性诚实性
    resilience_honesty = {
        "n_scenarios": result["resilience"]["n_scenarios"],
        "n_active": agg["n_active"], "n_skipped": agg["n_skipped"],
        "n_failed": agg["n_failed"], "n_unproven": agg["n_unproven"],
        "censoring_rate": agg["censoring_rate"],
        "t_island_mean_is_lower_bound": agg["t_island_mean_is_lower_bound"],
        "eens": agg["eens"], "t_island_mean": agg["t_island_mean"],
        "n1_pass_rate": agg["n1_pass_rate"],
        "warnings": result["resilience"]["warnings"],
        "initial_soc_source": result["resilience"]["initial_soc_source"],
        "note": ("t_island_mean 为右删失下界均值；据此的 f3 是上界估计。"
                 "若 censoring_rate 高，f3 对储能定容可能不可分辨——排序使用前"
                 "须先延长故障时长或完成标定（沿用 M2.5 结论）"),
    }

    # ---- 产物落盘
    traj = result.pop("trajectory")
    traj.to_parquet(OUT / f"high_{arm_id}_annual_trajectory.parquet")
    result["resilience"]["by_scenario"].to_csv(OUT / f"high_{arm_id}_by_scenario.csv", index=False)
    result["resilience"].pop("by_scenario")
    hashes_after = config_hashes()
    unchanged = {p: hashes_after[p] == frozen.get(p) for p in hashes_after}
    entry = {
        "stage": "M3-high-fidelity", "spec_version": "2.9", "simulation": True,
        "arm": arm_id, "sizing": sizing, "strategy": STRATEGY, "region": REGION,
        "seed": SEED, "status": result["status"], "elapsed_s": elapsed,
        "objectives_minimize": {
            "f1_LCOE_yuan_per_mwh": result["LCOE"],
            "f2_carbon_per_mtok_kgco2": result["carbon_per_mtok"],
            "f3_resilience_gap": result["resilience_gap"],
            "f4_sla_violation": 1.0 - result["sla_rate"],
        },
        "sla_rate": result["sla_rate"],
        "cross_arm_indicators": {"LCOE": result["LCOE"],
                                 "net_annual_cost": result["net_annual_cost"],
                                 "total_annualized_cost_per_mtok":
                                     result["total_annualized_cost_per_mtok"]},
        "metrics": result["metrics"], "annualization": result["annualization"],
        "hand_checks": checks, "resilience_honesty": resilience_honesty,
        "windows": [{k: v for k, v in w.items() if k != "metrics"} for w in windows],
        "notes": result["notes"],
        "configs_unchanged_from_m1": unchanged,
        "all_configs_unchanged": all(unchanged.values()),
        "baseline_s_base_unchanged": baseline_hash == frozen_m29["src/aidc/model/_baseline.py"],
        "configs_before": hashes_before, "configs_after": hashes_after,
    }
    write(OUT / f"report_high_{arm_id}.json", entry)
    bad = [k for k, c in checks.items() if not c["ok"]]
    print(json.dumps({"arm": arm_id, "status": result["status"],
                      "objectives": entry["objectives_minimize"],
                      "resilience": {k: resilience_honesty[k] for k in
                                     ("n_active", "n_skipped", "n_unproven", "censoring_rate",
                                      "t_island_mean", "eens", "n1_pass_rate")},
                      "hand_checks_failed": bad,
                      "all_configs_unchanged": entry["all_configs_unchanged"],
                      "elapsed_s": elapsed}, ensure_ascii=False, default=clean, indent=2),
          flush=True)
    assert not bad, f"hand checks failed: {bad}"
    assert entry["all_configs_unchanged"] and entry["baseline_s_base_unchanged"]


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "A3")
