"""M3 低保真闭环验证：单配置四目标（f3 = NaN，§4.2）+ A0 退化 + 手算对照。

跑两条臂（A0 无储能退化基线、A3 钠锂分层），策略 S-cost（生产策略），
R-NW，种子 20260816。所有目标值用**独立于实现的手算公式**从返回的
逐窗总量复核；11 份 YAML 与 _baseline.py 的哈希必须与 M1 冻结基线一致；
dispatch.py 的改动（normal_soc_boundary 扩展）单独声明并核对缺省行为不变
（由 tests/test_sizing_objective.py 的边界测试守门）。

用法：
    python -B outputs/m3_validation/validate_m3_low.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from aidc.data.chemistry import chemistry_params_from_config, load_config_bundle  # noqa: E402
from aidc.sizing.objective import evaluate_sizing  # noqa: E402

import pandas as pd  # noqa: E402
from aidc.data.tasks import TASK_COLUMNS  # noqa: E402

SEED = 20260816
REGION = "R-NW"
STRATEGY = "S-cost"


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (np.ndarray, list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, pd.DataFrame):
        return clean(value.to_dict(orient="records"))
    return value


def write(path: Path, value) -> None:
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def config_hashes() -> dict:
    paths = sorted((ROOT / "configs").rglob("*.yaml"))
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths}


def hand_checks(result: dict, bundle: dict) -> dict:
    """用独立手算公式复核全部四个目标与年化口径（不复读实现代码）。"""
    checks: dict = {}
    weights = result["annualization"]["weights"]
    factor = result["annualization"]["year_factor"]
    windows = result["windows"]

    def hand(key):
        return sum(w["totals"][key] * weights[w["window_id"]] for w in windows) * factor

    served = hand("energy_served_mwh")
    op = hand("operating_cost_yuan")
    carbon = hand("carbon_net_kg")
    tokens = hand("tokens_mtok")
    delay = hand("delay_count")
    n_tasks = hand("n_tasks")
    cycles = {"Na": hand("equivalent_cycles_Na"), "Li": hand("equivalent_cycles_Li")}

    finance = bundle["economics"]["finance"]
    rate, years = float(finance["discount_rate"]), float(finance["project_life_years"])
    crf = rate * (1 + rate) ** years / ((1 + rate) ** years - 1)
    storage = 0.0
    for name in ("Na", "Li"):
        chem = chemistry_params_from_config(bundle[f"chemistry_{name.lower()}"])
        E, P = result["sizing"][f"E_{name}"], result["sizing"][f"P_{name}"]
        c = cycles[name]
        life = min(chem.calendar_life, chem.cycle_life / c if c > 0 else float("inf"))
        storage += (crf * (E * chem.unit_cost_energy + P * chem.unit_cost_power)
                    + E * chem.opex_annual
                    + E * chem.unit_cost_energy * max(0.0, 1.0 / life - 1.0 / years))

    lcoe = (op + storage) / served if served > 0 else float("inf")
    checks["annual_energy_served_mwh"] = {
        "hand": served, "reported": result["metrics"]["annual_energy_served_mwh"],
        "ok": bool(np.isclose(served, result["metrics"]["annual_energy_served_mwh"], rtol=1e-9))}
    checks["annual_operating_cost"] = {
        "hand": op, "reported": result["metrics"]["cost_total"],
        "ok": bool(np.isclose(op, result["metrics"]["cost_total"], rtol=1e-9))}
    checks["storage_annualized_cost"] = {
        "hand": storage, "reported": result["storage_annualized_cost"],
        "ok": bool(np.isclose(storage, result["storage_annualized_cost"], rtol=1e-9))}
    checks["LCOE_f1"] = {
        "hand": lcoe, "reported": result["LCOE"],
        "ok": bool(np.isclose(lcoe, result["LCOE"], rtol=1e-9))}
    checks["carbon_per_mtok_f2"] = {
        "hand": carbon / tokens, "reported": result["carbon_per_mtok"],
        "ok": bool(np.isclose(carbon / tokens, result["carbon_per_mtok"], rtol=1e-9))}
    checks["sla_rate_f4"] = {
        "hand": 1.0 - delay / n_tasks, "reported": result["sla_rate"],
        "ok": bool(np.isclose(1.0 - delay / n_tasks, result["sla_rate"], rtol=1e-9))}
    checks["resilience_gap_f3_is_nan_low_fidelity"] = {
        "reported": result["resilience_gap"],
        "ok": bool(np.isnan(result["resilience_gap"]))}
    checks["net_annual_cost"] = {
        "hand": op + storage, "reported": result["net_annual_cost"],
        "ok": bool(np.isclose(op + storage, result["net_annual_cost"], rtol=1e-9))}
    checks["total_annualized_cost_per_mtok"] = {
        "hand": (op + storage) / tokens, "reported": result["total_annualized_cost_per_mtok"],
        "ok": bool(np.isclose((op + storage) / tokens,
                              result["total_annualized_cost_per_mtok"], rtol=1e-9))}
    checks["weights_sum_to_one"] = {"sum": float(sum(weights.values())),
                                    "ok": bool(abs(sum(weights.values()) - 1.0) < 1e-9)}
    checks["all_windows_solved"] = {
        "statuses": [w["status"] for w in windows],
        "ok": len(windows) == 5 and all(w["status"] in ("feasible", "optimal") for w in windows)}
    return checks


def a0_degeneration_checks(result: dict) -> dict:
    """A0 退化：储能年化成本为 0、循环为 0、LCOE 仍有定义（§4.3）。"""
    return {
        "storage_annualized_cost_is_zero": {
            "reported": result["storage_annualized_cost"],
            "ok": result["storage_annualized_cost"] == 0.0},
        "equivalent_cycles_zero": {
            "Na": result["metrics"]["equivalent_cycles_Na"],
            "Li": result["metrics"]["equivalent_cycles_Li"],
            "ok": (result["metrics"]["equivalent_cycles_Na"] == 0.0
                   and result["metrics"]["equivalent_cycles_Li"] == 0.0)},
        "lcoe_finite_and_positive": {
            "reported": result["LCOE"],
            "ok": bool(np.isfinite(result["LCOE"]) and result["LCOE"] > 0)},
        "net_annual_cost_equals_operating": {
            "ok": bool(np.isclose(result["net_annual_cost"],
                                  result["metrics"]["cost_total"], rtol=1e-12))},
    }


def run_arm(arm_id: str, sizing: dict, bundle: dict) -> dict:
    print(f"START {arm_id} low-fidelity closed loop ({STRATEGY}, {REGION}, seed {SEED})", flush=True)
    started = time.perf_counter()
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    result = evaluate_sizing(deepcopy(sizing), REGION, empty, "low", STRATEGY, SEED,
                             ROOT / "configs")
    elapsed = time.perf_counter() - started
    entry = {
        "arm": arm_id, "sizing": sizing, "strategy": STRATEGY, "region": REGION,
        "seed": SEED, "fidelity": "low", "status": result["status"],
        "elapsed_s": elapsed,
        "objectives_minimize": {
            "f1_LCOE_yuan_per_mwh": result["LCOE"],
            "f2_carbon_per_mtok_kgco2": result["carbon_per_mtok"],
            "f3_resilience_gap": result["resilience_gap"],
            "f4_sla_violation": 1.0 - result["sla_rate"],
        },
        "sla_rate": result["sla_rate"],
        "cross_arm_indicators": {
            "LCOE": result["LCOE"],
            "net_annual_cost": result["net_annual_cost"],
            "total_annualized_cost_per_mtok": result["total_annualized_cost_per_mtok"],
        },
        "metrics": result["metrics"],
        "annualization": result["annualization"],
        "windows": result["windows"],
        "notes": result["notes"],
        "tasks_input_note": result["tasks_input_note"],
        "hand_checks": hand_checks(result, bundle),
    }
    if arm_id == "A0":
        entry["a0_degeneration"] = a0_degeneration_checks(result)
    write(OUT / f"low_{arm_id}.json", entry)
    n_ok = sum(1 for c in entry["hand_checks"].values() if c["ok"])
    print(f"DONE {arm_id}: status={result['status']} LCOE={result['LCOE']:.4f} "
          f"f2={result['carbon_per_mtok']:.4f} f3={result['resilience_gap']} "
          f"sla={result['sla_rate']:.6f} hand_checks {n_ok}/{len(entry['hand_checks'])} "
          f"elapsed={elapsed:.1f}s", flush=True)
    bad = [k for k, c in entry["hand_checks"].items() if not c["ok"]]
    assert not bad, f"{arm_id}: hand checks failed: {bad}"
    if arm_id == "A0":
        bad = [k for k, c in entry["a0_degeneration"].items() if not c["ok"]]
        assert not bad, f"A0 degeneration failed: {bad}"
    return entry


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    bundle = load_config_bundle(ROOT / "configs")
    frozen = json.loads((ROOT / "outputs/m1_validation/protected_baseline.json")
                        .read_text(encoding="utf-8"))
    frozen_m29 = json.loads((ROOT / "outputs/m2_v29_validation/final_summary.json")
                            .read_text(encoding="utf-8"))["frozen_expected_sha256"]
    hashes_before = config_hashes()
    baseline_path = ROOT / "src/aidc/model/_baseline.py"
    baseline_hash = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    arms = {a["id"]: deepcopy(a["sizing_fixed"]) for a in bundle["experiments"]["arms"]}
    report = {
        "stage": "M3-low-fidelity",
        "spec_version": "2.9",
        "simulation": True,
        "configs_before": hashes_before,
        "baseline_s_base_unchanged": baseline_hash == frozen_m29["src/aidc/model/_baseline.py"],
        "dispatch_extension_note": (
            "dispatch.py 增加可选 cfg['normal_soc_boundary']（滚动时域 SOC 传递，SPEC §13.1）；"
            "缺省行为与原契约完全一致（soc(0)=soc(T)=soc_init），由 "
            "tests/test_sizing_objective.py::test_normal_soc_boundary_* 守门；"
            "本次低保真验证未使用该键"),
        "arms": {},
    }
    for arm_id in ("A0", "A3"):
        report["arms"][arm_id] = run_arm(arm_id, arms[arm_id], bundle)
    report["configs_after"] = config_hashes()
    # frozen（M1）键用反斜杠；统一成正斜杠比较
    normalized_frozen = {k.replace("\\", "/"): v for k, v in frozen.items()}
    report["configs_unchanged_from_m1"] = {
        path: digest == normalized_frozen.get(path)
        for path, digest in report["configs_after"].items()}
    report["all_configs_unchanged"] = all(report["configs_unchanged_from_m1"].values())
    assert report["all_configs_unchanged"], "configs changed during validation"
    assert report["baseline_s_base_unchanged"], "_baseline.py changed"
    write(OUT / "report_low.json", report)
    print(json.dumps({"all_configs_unchanged": report["all_configs_unchanged"],
                      "baseline_s_base_unchanged": report["baseline_s_base_unchanged"],
                      "arms": {a: report["arms"][a]["objectives_minimize"]
                               for a in report["arms"]}},
                     ensure_ascii=False, default=clean, indent=2), flush=True)


if __name__ == "__main__":
    main()
