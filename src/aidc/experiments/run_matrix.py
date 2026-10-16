"""主实验矩阵：4 臂 × 5 策略 × 5 种子（SPEC §6.3）。

M4 实现口径（BT2 裁定，CHANGELOG v2.10）：

- **低保真代表周路径**：每组 = `evaluate_sizing(fidelity="low")`——
  5 个 168 h 代表周（W-WIN/W-WINX/W-SPR/W-SUM/W-AUT，覆盖四季 + 极端低温周），
  权重 0.18/0.16/0.22/0.22/0.22，年化 = Σ 周结果 × weight × 365/7。
  §6.3 允许"季节轮次"或"全年高保真"两条路径，BT2 裁定选代表周路径：
  高保真矩阵需 35–87 h，且归一化试点口径就是低保真（口径一致）。
  **路径选择写入 matrix_metadata.json 与导出表头注记（§6.3 要求报告写明）。**
- `season_rounds` / `horizon_hours` 保留签名，仅接受缺省值（4 / 168）；
  其他取值报错并指向本裁定，不静默忽略。
- 每组记录：配置、策略参数（S-balance 的权重与 w_sla）、种子、求解状态、
  逐窗认证 gap 与耗时、逐窗不变量残差；**infeasible 组原样记录，不跳过**。
- `sizing_source = "config_fixed_candidate"`（experiments.yaml 的 sizing_fixed，
  A1/A2/A3 统一 150 MWh / 45 MW，规模可比）；M5 后如改用外层优化结果，
  必须改标 `"outer_optimization"`。
- 并行确定性：每组种子由矩阵定义**显式给出**（不派生、不使用 Python 内置
  `hash`）；求解器单线程（§9.39），进程间并行不改变单解确定性。
- f3（resilience_gap）在低保真为 NaN（§4.2）；表格另列**臂级**韧性列，
  取自 M2.5 场景包（outputs/m25_validation/resilience/report.json，故障调度与策略无关，
  故按臂合并），并标注**不可分辨（non-discriminating）**：当前占位数据下
  孤岛观测全部右删失，该列**不得参与任何排序**（BT2 附带的常设警示）。
- BT2-③：任一周窗认证 gap > 2% → 该组 `provisional_for_comparison = True`；
  跨臂对比若涉及此类组，结论标注为临时。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
from copy import deepcopy
from itertools import product
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

from aidc.data.tasks import TASK_COLUMNS
from aidc.experiments._checks import check_week_invariants
from aidc.model.storage import StorageSpec, capacity_fade
from aidc.sizing.objective import evaluate_sizing

SEASON_LABEL = "annualized_5_repr_weeks"
SIZING_SOURCE = "config_fixed_candidate"
PROVISIONAL_GAP_THRESHOLD = 0.02          # BT2-③：认证 gap > 2% → 临时结论
RESILIENCE_SOURCE_NOTE = ("M2.5 scenario package (outputs/m25_validation/resilience/report.json, "
                          "seed 20260816, W-WINX week, spec v2.9); fault dispatch is "
                          "strategy-independent (degradation policy), merged per arm; "
                          "NON-DISCRIMINATING: all island observations right-censored")

_W: dict = {}


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, pd.DataFrame):
        return "[dataframe omitted]"
    if isinstance(value, (np.ndarray, list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(_clean(value), ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def _init_worker(config_dir: str, region: str, output_dir: str) -> None:
    from aidc.data.chemistry import chemistry_params_from_config, load_config_bundle
    cfg = load_config_bundle(Path(config_dir))
    cfg["region"] = deepcopy(cfg["regions"][region])
    chem = {name: chemistry_params_from_config(cfg[f"chemistry_{name.lower()}"])
            for name in ("Na", "Li")}
    arms = {a["id"]: deepcopy(a["sizing_fixed"]) for a in cfg["experiments"]["arms"]}
    resilience: dict = {}
    res_path = Path(config_dir).parent / "outputs/m25_validation/resilience/report.json"
    if res_path.exists():
        report = json.loads(res_path.read_text(encoding="utf-8"))
        for arm_id, arm in report.get("arms", {}).items():
            agg = arm.get("aggregate", {})
            resilience[arm_id] = {k: agg.get(k) for k in
                                  ("t_island_mean", "t_island_worst", "censoring_rate",
                                   "eens", "n1_pass_rate", "n_active", "n_skipped")}
    strategies = {s["id"]: deepcopy(s) for s in cfg["experiments"]["strategies"]}
    _W.update(cfg=cfg, chem=chem, arms=arms, resilience=resilience, region=region,
              config_dir=Path(config_dir), output_dir=Path(output_dir), strategies=strategies)


def _investment(sizing: dict) -> float:
    """每臂投资额（元，名义总投资）= Σ E·unit_cost_energy + P·unit_cost_power。"""
    total = 0.0
    for name in ("Na", "Li"):
        chem = _W["chem"][name]
        total += (sizing[f"E_{name}"] * chem.unit_cost_energy
                  + sizing[f"P_{name}"] * chem.unit_cost_power)
    return float(total)


def _fade(name: str, sizing: dict, annual_cycles: float) -> float:
    """容量衰减估算（1 年运行口径，§10：必须标注为估算；不进成本侧）。"""
    if sizing[f"E_{name}"] <= 0:
        return float("nan")     # 零容量体系的衰减无定义，不用日历项冒充
    spec = StorageSpec(name=name, chem=_W["chem"][name],
                       E_nom=sizing[f"E_{name}"], P_nom=sizing[f"P_{name}"])
    return capacity_fade(max(0.0, annual_cycles), 1.0, spec)


def _f(value) -> float:
    """None/NaN 统一为 float NaN，保证韧性数值列为 float dtype。"""
    if value is None:
        return float("nan")
    value = float(value)
    return value if np.isfinite(value) else float("nan")


def _run_combo(payload: tuple[str, str, int]) -> dict:
    arm_id, strategy, seed = payload
    cfg, sizing = _W["cfg"], deepcopy(_W["arms"][arm_id])
    started = perf_counter()
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    result = evaluate_sizing(sizing, _W["region"], empty, "low", strategy, seed,
                             _W["config_dir"], keep_week_results=True)
    elapsed = perf_counter() - started

    # ---- 逐窗不变量（§9 轨迹级；全部窗口都必须通过）
    week_checks = []
    for week in (result.get("week_results") or []):
        check = check_week_invariants(week["trajectory"], week["ts"], week["tasks"],
                                      week["task_schedule"], cfg, sizing)
        week_checks.append({"week_id": week["week_id"], **check})
    invariants_ok = bool(week_checks) and all(c["all_ok"] for c in week_checks)
    max_soc_residual = max((c["soc_dynamic_residual"] for c in week_checks), default=float("nan"))

    windows = result["windows"]
    gaps = [w.get("certified_gap") if w.get("certified_gap") is not None else w.get("mip_gap")
            for w in windows]
    gaps = [g for g in gaps if g is not None and np.isfinite(g)]
    gap_unknown = sum(1 for w in windows
                      if w["status"] not in ("skipped",)
                      and (w.get("mip_gap") is None or not np.isfinite(w.get("mip_gap"))))
    certified = [w.get("certificate_target_met") for w in windows]
    n_cert_targets = sum(1 for c in certified if c is not None)
    provisional = bool(gaps) and max(gaps) > PROVISIONAL_GAP_THRESHOLD

    guard_fired = None
    if strategy == "S-balance":
        fired = [bool((w.get("solver_info_subset") or {}).get("s_balance_normalization_guard", {})
                      .get("fired", False))
                 for w in (result.get("week_results") or [])]
        guard_fired = any(fired)

    metrics = result["metrics"]
    res = _W["resilience"].get(arm_id, {})
    feasible = result["status"] in ("feasible", "optimal")
    row = {
        "arm": arm_id, "strategy": strategy, "seed": seed, "season": SEASON_LABEL,
        "sizing_source": SIZING_SOURCE,
        **{k: sizing[k] for k in ("E_Na", "P_Na", "E_Li", "P_Li")},
        "investment_yuan": _investment(sizing),
        "status": result["status"],
        "LCOE": result["LCOE"] if feasible else float("nan"),
        "net_annual_cost": result["net_annual_cost"] if feasible else float("nan"),
        "total_annualized_cost_per_mtok": result["total_annualized_cost_per_mtok"] if feasible else float("nan"),
        "cost_per_mtok": metrics.get("cost_per_mtok", float("nan")),
        "carbon_per_mtok": result["carbon_per_mtok"],
        "green_matching_rate": metrics.get("green_matching_rate", float("nan")),
        "curtailment_rate": metrics.get("curtailment_rate", float("nan")),
        "sla_rate": result["sla_rate"],
        "delay_count": metrics.get("delay_count", float("nan")),
        "latency_p50_h": metrics.get("latency_p50", float("nan")),
        "latency_p90_h": metrics.get("latency_p90", float("nan")),
        # f3：低保真为 NaN（§4.2）——不可分辨列，禁止参与排序（BT2 常设警示）
        "resilience_gap": result["resilience_gap"],
        "resilience_gap_note": "NaN at low fidelity by SPEC §4.2; arm-level resilience "
                               "columns come from the M2.5 package and are NON-DISCRIMINATING",
        "t_island_mean_h": _f(res.get("t_island_mean")),
        "t_island_worst_h": _f(res.get("t_island_worst")),
        "censoring_rate": _f(res.get("censoring_rate")),
        "eens_mwh": _f(res.get("eens")),
        "n1_pass_rate": _f(res.get("n1_pass_rate")),
        "resilience_source": RESILIENCE_SOURCE_NOTE if res else "unavailable",
        "equivalent_cycles_Na": metrics.get("equivalent_cycles_Na", float("nan")),
        "equivalent_cycles_Li": metrics.get("equivalent_cycles_Li", float("nan")),
        "capacity_fade_Na_1y_est": _fade("Na", sizing, metrics.get("equivalent_cycles_Na", 0.0) or 0.0),
        "capacity_fade_Li_1y_est": _fade("Li", sizing, metrics.get("equivalent_cycles_Li", 0.0) or 0.0),
        "q_dump_total": metrics.get("q_dump_total", float("nan")),
        "q_unused_total": metrics.get("q_unused_total", float("nan")),
        "heat_from_waste_share": metrics.get("heat_from_waste_share", float("nan")),
        "n_windows": len(windows),
        "windows_certified_frac": (sum(1 for c in certified if c) / n_cert_targets)
        if n_cert_targets else None,
        "windows_gap_unknown": gap_unknown,
        "max_certified_gap": max(gaps) if gaps else None,
        "provisional_for_comparison": provisional,
        "s_balance_guard_fired": guard_fired,
        "invariants_all_ok": invariants_ok,
        "invariants_max_soc_residual": max_soc_residual,
        "elapsed_s": elapsed,
    }

    # ---- 每组完整记录（含逐窗求解与不变量细节；轨迹不入库，核查后即弃）
    record = {
        "row": row,
        "arm": arm_id, "strategy": strategy, "seed": seed, "region": _W["region"],
        "sizing": sizing, "sizing_source": SIZING_SOURCE,
        "strategy_params": _W["strategies"].get(strategy, {}),
        "status": result["status"], "fidelity": "low",
        "objectives_minimize": {"f1_LCOE": result["LCOE"], "f2_carbon_per_mtok": result["carbon_per_mtok"],
                                "f3_resilience_gap": result["resilience_gap"],
                                "f4_sla_violation": (1.0 - result["sla_rate"])
                                if np.isfinite(result["sla_rate"]) else float("nan")},
        "metrics": metrics,
        "windows": [{k: v for k, v in w.items() if k != "metrics"} for w in windows],
        "week_invariants": week_checks,
        "notes": result["notes"],
        "annualization": result["annualization"],
        "elapsed_s": elapsed,
    }
    matrix_dir = _W["output_dir"] / "matrix"
    matrix_dir.mkdir(parents=True, exist_ok=True)
    _write_json(matrix_dir / f"{arm_id}_{strategy}_{seed}.json", record)
    return row


def _record_path(output_dir: Path, arm_id: str, strategy: str, seed: int) -> Path:
    return Path(output_dir) / "matrix" / f"{arm_id}_{strategy}_{seed}.json"


def _load_existing_row(output_dir: Path, arm_id: str, strategy: str, seed: int) -> dict | None:
    path = _record_path(output_dir, arm_id, strategy, seed)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        row = record.get("row")
        return row if isinstance(row, dict) and "status" in row else None
    except (json.JSONDecodeError, OSError):
        return None


def run_matrix(
    region: str,
    config_dir: Path,
    output_dir: Path,
    arms: list[str] | None = None,
    strategies: list[str] | None = None,
    seeds: list[int] | None = None,
    season_rounds: int = 4,
    horizon_hours: int = 168,
    parallel: bool = True,
    n_workers: int | None = None,
) -> pd.DataFrame:
    """跑完 4 臂 × 5 策略 × 5 种子 = 100 组实验。

    Args:
        region: 地区画像 ID（主场景建议先跑 R-NW）
        config_dir: configs 目录
        output_dir: 结果输出目录（每组一个子目录 + 一个汇总 CSV）
        arms / strategies / seeds: None 时取 experiments.yaml 的定义
        season_rounds: 季节轮次，168h × 4 覆盖四季
        horizon_hours: 每组评估窗口长度
        parallel: 是否并行
        n_workers: 并行进程数

    Returns:
        汇总结果表，每行一组实验，列为：
        arm, strategy, seed, season, sizing_*, LCOE, carbon_per_mtok,
        resilience_gap, sla_rate, 以及 SPEC §6.4 的全部指标

    Implementation notes:
        - **每一组都必须记录**：所用配置、策略权重、种子、求解器状态、耗时。
          任何一组 infeasible 都要保留记录并标注，不能跳过。
        - 配置臂如何取得 sizing：A0 固定为 0；A1/A2/A3 需先跑外层优化（M5），
          或用 experiments.yaml 中给出的固定配置（便于 M4 阶段先跑通）。
          **两种来源必须在结果表中标注**（列 sizing_source）。
        - 并行时确保确定性：每个 (arm, strategy, seed, season) 组合独立设种子。
        - 季节轮次的选择需覆盖冬季（低温是核心机制），且需在结果中可区分。

    M4 实现说明（BT2 裁定）：本实现走 §4.2 低保真代表周路径——5 个 168 h
    代表周已覆盖四季并单列极端低温周 W-WINX（比"4 个季节轮次"多一个极端周，
    且与归一化试点口径一致）。`season_rounds` / `horizon_hours` 仅接受缺省值，
    其余取值报错而不是静默忽略。断点续跑：已存在完整记录的组直接加载。
    """
    if season_rounds != 4 or horizon_hours != 168:
        raise ValueError(
            "M4 按 BT2 裁定走 §4.2 低保真代表周路径（5×168h，覆盖四季 + W-WINX）；"
            f"不支持 season_rounds={season_rounds}/horizon_hours={horizon_hours} 的自定义取值")
    from aidc.data.chemistry import load_config_bundle
    cfg = load_config_bundle(Path(config_dir))
    matrix = cfg["experiments"]["matrix"]
    arms = list(arms) if arms else list(matrix["arms"])
    strategies = list(strategies) if strategies else list(matrix["strategies"])
    seeds = [int(s) for s in seeds] if seeds else [int(s) for s in matrix["seeds"]]
    unknown_a = set(arms) - set(matrix["arms"])
    unknown_s = set(strategies) - set(matrix["strategies"])
    if unknown_a or unknown_s:
        raise ValueError(f"未知臂 {sorted(unknown_a)} 或策略 {sorted(unknown_s)}")
    output_dir = Path(output_dir)
    (output_dir / "matrix").mkdir(parents=True, exist_ok=True)
    combos = [(a, s, sd) for a, s, sd in product(arms, strategies, seeds)]

    rows: list[dict] = []
    pending: list[tuple[str, str, int]] = []
    for combo in combos:
        existing = _load_existing_row(output_dir, *combo)
        if existing is not None:
            rows.append(existing)
        else:
            pending.append(combo)

    started = perf_counter()
    if pending:
        if parallel and len(pending) > 1:
            workers = int(n_workers or min(6, os.cpu_count() or 2))
            with mp.Pool(workers, initializer=_init_worker,
                         initargs=(str(Path(config_dir).resolve()), region,
                                   str(output_dir.resolve()))) as pool:
                rows.extend(pool.imap(_run_combo, pending, chunksize=1))
        else:
            if not _W:
                _init_worker(str(Path(config_dir).resolve()), region, str(output_dir.resolve()))
            for combo in pending:
                rows.extend([_run_combo(combo)])
                print(f"  [{len(rows)}/{len(combos)}] {combo} done "
                      f"({perf_counter() - started:.0f}s)", flush=True)

    order = {(a, s, sd): i for i, (a, s, sd) in enumerate(combos)}
    rows.sort(key=lambda r: order[(r["arm"], r["strategy"], r["seed"])])
    summary = pd.DataFrame(rows)
    summary.to_csv(output_dir / "matrix_summary.csv", index=False)

    n_infeasible = int((summary["status"] != "feasible").sum()
                       if "status" in summary else 0)
    metadata = {
        "path_choice": "低保真代表周路径（§4.2：5×168h，权重 0.18/0.16/0.22/0.22/0.22，"
                       "覆盖四季 + 极端低温周 W-WINX）。§6.3 的两条路径之外按 BT2 裁定选用，"
                       "与 S-balance 归一化试点口径一致；高保真矩阵需 35–87 h 故不采用",
        "region": region, "n_combos": len(summary),
        "n_infeasible_recorded_not_skipped": n_infeasible,
        "n_provisional_gap_over_2pct": int(summary["provisional_for_comparison"].sum())
        if "provisional_for_comparison" in summary else 0,
        "s_balance_guard_fired_combos": int((summary["s_balance_guard_fired"] == True).sum())  # noqa: E712
        if "s_balance_guard_fired" in summary else 0,
        "invariants_all_ok": bool(summary["invariants_all_ok"].all()) if len(summary) else None,
        "f3_policy": "resilience_gap = NaN（低保真，§4.2）；臂级韧性列来自 M2.5 场景包，"
                     "全部右删失 → NON-DISCRIMINATING，不参与任何排序（BT2 常设警示）",
        "sizing_source": SIZING_SOURCE,
        "scale_normalization": "A1/A2/A3 统一 150 MWh / 45 MW（§6.1 规模可比）；A0 投资为 0，"
                               "跨臂比较须结合 investment_yuan 列说明",
        "elapsed_s": perf_counter() - started,
    }
    _write_json(output_dir / "matrix_metadata.json", metadata)
    return summary


def load_matrix_results(output_dir: Path) -> pd.DataFrame:
    """加载已有结果，便于断点续跑与事后分析。"""
    path = Path(output_dir) / "matrix_summary.csv"
    if not path.exists():
        raise FileNotFoundError(f"矩阵汇总不存在：{path}（先运行 run_matrix）")
    return pd.read_csv(path)
