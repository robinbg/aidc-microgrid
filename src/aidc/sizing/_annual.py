"""滚动时域年度调度与年度韧性接线（SPEC §13.1、§4.2、§5）。

`aidc.sizing.objective.evaluate_sizing(fidelity="high")` 的私有支撑模块。

本模块**不放宽任何求解精度、不缩短任务窗口、不改变降级策略**：
年度段按连续窗口滚动求解，SOC 逐窗传递；每个窗口用同一组生成参数与
种子按窗口长度重新生成任务（§4.2 的电量口径自动完成规模缩放）；每个
窗口的可行性单独检查并记录，首个不可行窗口即停止滚动（§13.1：
不可行窗口会导致后续全部不可行，不得静默级联）。

韧性场景在年度序列上采样；每个场景的初始 SOC 取年度正常调度在故障
起始小时的真实 SOC（`fault_initial_soc_mode = from_normal_end` 的
年度口径）。故障模式下任务全部暂停，`build_dispatch_model` 会忽略
任务表（`tasks.iloc[:0]`），因此显式传入空表而不是年度大表。
"""

from __future__ import annotations

import warnings
from copy import deepcopy
from time import perf_counter

import numpy as np
import pandas as pd

from aidc.data.chemistry import ChemistryParams, chemistry_params_from_config
from aidc.data.tasks import TASK_COLUMNS, generate_tasks
from aidc.model.dispatch import solve_dispatch
from aidc.resilience import aggregate_resilience, build_scenarios, evaluate_scenario

WEEK_HOURS = 168


def rolling_window_lengths(total_hours: int, week_hours: int = WEEK_HOURS) -> list[int]:
    """把年度段切分为连续窗口；不足一窗的余量并入最后一窗。

    8760 → 51 个 168 h 窗口 + 1 个 192 h 窗口。并入而不是丢弃或拼接，
    保证每个窗口都不短于 168 h——S3 容忍窗口最大 24 h，更短的窗口
    容纳不下 `arrive + duration + tolerance`（SPEC §4.2 的同一理由）。
    """
    total_hours, week_hours = int(total_hours), int(week_hours)
    if total_hours <= 0 or week_hours <= 0:
        raise ValueError("total_hours 与 week_hours 必须为正")
    if total_hours < week_hours:
        raise ValueError("年度段短于单个窗口，无法滚动")
    n_full, remainder = divmod(total_hours, week_hours)
    if remainder == 0:
        return [week_hours] * n_full
    return [week_hours] * (n_full - 1) + [week_hours + remainder]


def _chemistry(cfg: dict, name: str) -> ChemistryParams:
    value = cfg[f"chemistry_{name.lower()}"]
    return value if isinstance(value, ChemistryParams) else chemistry_params_from_config(value)


def generation_params(cfg: dict, region_cfg: dict, tasks: pd.DataFrame | None) -> dict:
    """汇总任务生成参数：柔性比与 S3 容忍窗口。

    柔性比优先取传入任务表 attrs 的 `flexible_ratio_target`（调用方口径），
    否则取画像 `compute_override.flexible_task_ratio`。容忍窗口按画像取
    `experiments.tasks.s3_tolerance_hours_by_region`，缺省回退默认值。
    """
    region_id = region_cfg["region_id"]
    override = region_cfg.get("compute_override", {})
    if "flexible_task_ratio" not in override:
        raise ValueError(f"{region_id}: 缺少 compute_override.flexible_task_ratio")
    ratio = float(override["flexible_task_ratio"])
    if tasks is not None:
        attrs = getattr(tasks, "attrs", None) or {}
        if "flexible_ratio_target" in attrs:
            ratio = float(attrs["flexible_ratio_target"])
    tasks_cfg = cfg["experiments"]["tasks"]
    tolerance = float(tasks_cfg.get("s3_tolerance_hours_by_region", {}).get(
        region_id, tasks_cfg["s3_tolerance_hours_default"]))
    if not 0 < ratio < 1:
        raise ValueError("flexible_task_ratio 必须在 (0, 1) 内")
    return {"flexible_ratio": ratio, "s3_tolerance_hours": tolerance}


def regenerate_window_tasks(window_ts: pd.DataFrame, gen: dict, cfg: dict, seed: int) -> pd.DataFrame:
    """按窗口电量口径重新生成任务（SPEC §4.2：不映射主任务）。

    任务规模由 E_S1（窗口内 IT_base 电量）与目标柔性比反解，
    因此窗口长度变化时任务数自动等比例缩放。
    """
    return generate_tasks(
        float(window_ts["IT_base"].sum()), len(window_ts),
        int(window_ts.attrs["gpu_capacity_plan"]), gen["flexible_ratio"], seed,
        float(window_ts.attrs["gpu_power_mw_per_card"]), gen["s3_tolerance_hours"],
        cfg["generation"])


def _solver_summary(result) -> dict:
    info = result.solver_info or {}
    certificate = info.get("normal_bound_certificate")
    return {
        "status": result.status,
        "time_s": info.get("time_s"),
        "mip_gap": info.get("mip_gap"),
        "backend": info.get("backend"),
        "optimized": info.get("optimized", True),
        "level1_proven": info.get("level1_proven"),
        "certified_gap": certificate.get("global_mip_gap") if certificate else None,
        "certificate_target_met": certificate.get("target_met") if certificate else None,
    }


def _aggregate_status(statuses: list[str]) -> str:
    if any(status == "infeasible" for status in statuses):
        return "infeasible"
    if any(status in ("error", "unbounded", "feasible_not_proven") for status in statuses):
        return "feasible_not_proven" if "feasible_not_proven" in statuses else "error"
    if statuses and all(status == "optimal" for status in statuses):
        return "optimal"
    return "feasible"


def rolling_annual_dispatch(ts_annual: pd.DataFrame, cfg: dict, sizing: dict, strategy: str,
                            seed: int, *, time_limit_s: float = 60.0, mip_gap: float = 0.01,
                            tasks: pd.DataFrame | None = None) -> dict:
    """滚动时域求解年度正常调度，SOC 逐窗传递（SPEC §13.1）。

    - 只有第一个窗口要求 `soc(0) = soc_init`；只有最后一个窗口要求
      `soc(T_end) = soc_init`；中间窗口终端自由（通过
      `cfg["normal_soc_boundary"]` 传给 `solve_dispatch`，缺省行为不变）。
    - 每窗任务用同一组生成参数与种子按窗口电量口径重新生成。
    - 每窗求解状态、gap、认证信息、SOC 衔接值逐窗记录；首个不可行
      窗口之后的窗口标记为 skipped，不再求解。
    """
    if not isinstance(ts_annual.index, pd.DatetimeIndex) or ts_annual.empty:
        raise ValueError("滚动时域需要非空 DatetimeIndex 输入")
    lengths = rolling_window_lengths(len(ts_annual))
    region_cfg = cfg.get("region")
    if region_cfg is None:
        region_cfg = cfg["regions"][ts_annual.attrs["region_id"]]
    gen = generation_params(cfg, region_cfg, tasks)
    soc_init = {name: float(_chemistry(cfg, name).soc_init) for name in ("Na", "Li")}
    windows, trajectories = [], []
    soc_pieces = {name: [] for name in ("Na", "Li")}
    carry: dict | None = None
    position = 0
    started = perf_counter()
    stopped_early = False
    for index, hours in enumerate(lengths):
        last = index == len(lengths) - 1
        record = {"index": index, "start_h": position, "hours": hours, "last": last}
        if stopped_early:
            record.update(status="skipped", note="前序窗口不可行；滚动停止（SPEC §13.1）")
            windows.append(record)
            continue
        window = ts_annual.iloc[position:position + hours].copy()
        window.attrs = deepcopy(ts_annual.attrs)
        window.attrs.update(window_index=index, window_start_h=position, independent_window=False)
        tasks_window = regenerate_window_tasks(window, gen, cfg, seed)
        window_cfg = deepcopy(cfg)
        window_cfg["region"] = region_cfg
        boundary: dict = {}
        if carry is not None:
            boundary["initial"] = dict(carry)
        if last:
            boundary["terminal"] = dict(soc_init)
        else:
            boundary["terminal_free"] = True
        window_cfg["normal_soc_boundary"] = boundary
        result = solve_dispatch(window, tasks_window, window_cfg, sizing, strategy, seed,
                                time_limit_s=time_limit_s, mip_gap=mip_gap, mode="normal")
        record.update(_solver_summary(result), n_tasks=int(len(tasks_window)),
                      soc_initial_requested=boundary.get("initial"),
                      soc_terminal=dict(result.trajectories.attrs.get("soc_terminal", {}))
                      if not result.trajectories.empty else None)
        if result.status == "infeasible":
            record["note"] = "窗口不可行；原样上报，不得当作 0 成本（SPEC §9.25）"
            windows.append(record)
            stopped_early = True
            continue
        if result.trajectories.empty or result.status in ("error", "unbounded"):
            record["note"] = f"窗口求解未返回轨迹（status={result.status}）"
            windows.append(record)
            stopped_early = True
            continue
        trajectory = result.trajectories
        record["metrics"] = deepcopy(result.metrics)
        record["soc_initial"] = deepcopy(trajectory.attrs.get("soc_initial", {}))
        windows.append(record)
        for name in ("Na", "Li"):
            soc_pieces[name].append(trajectory[f"soc_{name}"].to_numpy(dtype=float))
        trajectories.append(trajectory)
        carry = deepcopy(trajectory.attrs["soc_terminal"])
        position += hours
    trajectory = pd.concat(trajectories) if trajectories else pd.DataFrame()
    annual_soc = {name: np.concatenate(parts) if parts else np.empty(0)
                  for name, parts in soc_pieces.items()}
    statuses = [w["status"] for w in windows if w["status"] != "skipped"]
    return {
        "status": _aggregate_status(statuses),
        "windows": windows,
        "trajectory": trajectory,
        "annual_soc": annual_soc,
        "n_windows": len(lengths),
        "n_solved": len(trajectories),
        "window_lengths": lengths,
        "soc_init": soc_init,
        "task_generation": gen,
        "elapsed_s": perf_counter() - started,
        "stopped_early": stopped_early,
    }


def annual_resilience(ts_annual: pd.DataFrame, annual_soc: dict, cfg: dict, sizing: dict,
                      seed: int) -> dict:
    """在年度序列上采样故障场景并聚合韧性指标（SPEC §5、§4.2 高保真）。

    - 场景由 `build_scenarios` 在 8760 h 上分层采样（同一配置与种子）。
    - 每个场景的故障初始 SOC = 年度正常调度在该小时的真实 SOC
      （`from_normal_end` 的年度口径；来源写入结果，报告中必须写明）。
    - 故障模式忽略任务表（可迁移任务全部暂停），显式传空表。
    - `aggregate_resilience` 的告警（删失比例、未证明比例、失败场景）
      原样保留进结果，不得静默。
    """
    scenarios = build_scenarios(ts_annual, cfg, seed)
    empty_tasks = pd.DataFrame(columns=TASK_COLUMNS)
    results = []
    for scenario in scenarios:
        scenario_cfg = deepcopy(cfg)
        start = int(scenario.start_h)
        # from_normal_end 的年度口径：故障起始小时的年度正常调度 SOC。
        # 对无储能体系也照常提供（_initial_soc 的来源解析要求该键存在；
        # E = 0 时调度侧不读取其值）。
        initial = {}
        for name in ("Na", "Li"):
            values = annual_soc.get(name)
            if values is None or len(values) <= start or not np.isfinite(values[start]):
                raise ValueError(f"annual SOC for {name} unavailable at scenario start {start}")
            initial[name] = float(values[start])
        scenario_cfg["initial_soc"] = initial
        results.append(evaluate_scenario(scenario, ts_annual, empty_tasks, scenario_cfg, sizing, seed))
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        aggregate = aggregate_resilience(results)
    by_scenario = aggregate.pop("by_scenario")
    return {
        "aggregate": aggregate,
        "by_scenario": by_scenario,
        "warnings": [str(w.message) for w in captured],
        "n_scenarios": len(scenarios),
        "initial_soc_source": "annual normal-dispatch SOC at each scenario start hour",
        "tasks_during_fault": "paused (fault mode ignores the task table by contract)",
    }
