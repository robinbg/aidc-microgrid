"""算力任务生成。

任务分三级（见 SPEC §3.1）：
    S1  在线推理类：不可迁移，已并入 IT_base 时间序列，**不进入任务表**。
    S2  微调类：可迁移，硬截止时间 deadline。
    S3  批量推理 / 训练类：可迁移，软截止，延迟计入 SLA 惩罚。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

Priority = Literal["S2", "S3"]
logger = logging.getLogger(__name__)

TASK_COLUMNS = [
    "task_id",       # str，唯一
    "priority",      # "S2" | "S3"（S1 已计入 IT_base）
    "gpu",           # int，GPU 卡数需求
    "duration_h",    # int，时长（小时）
    "arrive_h",      # int，相对窗口起点的小时索引
    "deadline_h",    # int，期望完成时刻（S2 为硬截止，S3 为期望时刻）
    "tolerance_h",   # float，S3 的容忍窗口（h）；S2 恒为 0（SPEC §3.1）
    "latest_h",      # = deadline_h + tolerance_h，最晚完成时刻
    "tokens_mtok",   # float，完成后产出的 Token 量（百万 Token）
]


def _finite_number(value: float, name: str, *, positive: bool = False) -> float:
    """拒绝非有限值及非法物理量，不修正调用方的输入。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not np.isfinite(number) or (number <= 0 if positive else number < 0):
        bound = "正数" if positive else "非负数"
        raise ValueError(f"{name} 必须是有限{bound}，实际为 {value!r}")
    return number


def _positive_integer(value: int, name: str) -> int:
    number = _finite_number(value, name, positive=True)
    if isinstance(value, (bool, np.bool_)) or not number.is_integer():
        raise ValueError(f"{name} 必须是正整数，实际为 {value!r}")
    return int(number)


def _load_default_config(filename: str) -> dict:
    # 保留现有 API：generation 可注入；Token 系数从 system 唯一真源读取。
    from aidc.data.chemistry import convert_units, load_yaml

    config_dir = Path(__file__).resolve().parents[3] / "configs"
    return convert_units(load_yaml(config_dir / filename))


def _distribution(config: Mapping, name: str) -> tuple[np.ndarray, np.ndarray]:
    """读取离散整数分布，返回取值与累计概率。"""
    values = np.asarray(config["values"], dtype=float)
    weights = np.asarray(config["weights"], dtype=float)
    if (
        values.ndim != 1
        or len(values) == 0
        or weights.shape != values.shape
        or not np.all(np.isfinite(values))
        or not np.all(values > 0)
        or not np.all(values == np.floor(values))
        or not np.all(np.isfinite(weights))
        or np.any(weights < 0)
        or weights.sum() <= 0
    ):
        raise ValueError(f"{name} 必须包含正整数 values 和匹配的非负 weights")
    cdf = np.cumsum(weights / weights.sum())
    cdf[-1] = 1.0
    return values.astype(np.int64), cdf


def _draw_discrete(
    rng: np.random.Generator, values: np.ndarray, cdf: np.ndarray
) -> int:
    return int(values[np.searchsorted(cdf, rng.random(), side="right")])


def check_flexible_ratio_feasible(
    it_base_mw: float,
    horizon_hours: int,
    gpu_capacity_plan: int,
    p_card_mw: float,
    target_ratio: float,
    max_required_avg_utilization: float = 0.60,
) -> dict:
    """柔性占比可达性预检（SPEC §2.2、§9.23）。

    在做任何采样之前先算上界。若不通过，**直接报错**——不得静默降低目标占比、
    不得调整 IT_base、不得放宽 GPU 容量。

        E_S1        = it_base_mw · horizon_hours
        E_tasks_req = E_S1 · R / (1 − R)
        E_tasks_max = gpu_capacity_plan · p_card_mw · horizon_hours
        u_req       = E_tasks_req / E_tasks_max

    若 u_req > max_required_avg_utilization，抛出 ValueError，信息须包含：
    目标 R、E_tasks 需求、可发电量上界、所需利用率、阈值。

    ⚠️ **通过此预检不等于任务集一定可调度**。
    这里的上界是"规划容量连续满载"的宽松上界，未计入到达分布、截止约束
    与非抢占限制。可调度性的真正验证在 M2 的求解阶段。
    报告中必须包含这一声明。

    背景：v1.5 的 `it_base_mw = 8.0` 与 `gpu_capacity_s1_reserved = 1024` 卡不自洽
    （1024 × 0.7 kW = 0.72 MW），使四个画像的目标占比全部不可达。
    v1.6 已把 IT_base 改为导出量（见 system.yaml），本预检是第二道防线。

    Returns:
        {"E_S1_mwh", "E_tasks_req_mwh", "E_tasks_max_mwh",
         "required_avg_utilization", "threshold", "feasible"}
    """
    base_mw = _finite_number(it_base_mw, "it_base_mw")
    horizon = _positive_integer(horizon_hours, "horizon_hours")
    capacity = _positive_integer(gpu_capacity_plan, "gpu_capacity_plan")
    p_card = _finite_number(p_card_mw, "p_card_mw", positive=True)
    ratio = _finite_number(target_ratio, "target_ratio")
    threshold = _finite_number(
        max_required_avg_utilization, "max_required_avg_utilization", positive=True
    )
    if ratio >= 1:
        raise ValueError("target_ratio 必须满足 0 <= R < 1")
    if threshold > 1:
        raise ValueError("max_required_avg_utilization 不能超过 1")
    if base_mw == 0 and ratio > 0:
        raise ValueError("E_S1 为 0 时无法定义非零且小于 1 的柔性电量占比")

    e_s1 = base_mw * horizon
    e_required = e_s1 * ratio / (1 - ratio)
    e_maximum = capacity * p_card * horizon
    utilization = e_required / e_maximum
    if not np.all(np.isfinite([e_s1, e_required, e_maximum, utilization])):
        raise ValueError("柔性占比可达性预检产生非有限电量或利用率")
    if utilization > threshold:
        raise ValueError(
            f"柔性占比可达性预检失败：目标 R={ratio:.12g}，"
            f"E_tasks 需求={e_required:.12g} MWh，"
            f"规划容量可发电量上界={e_maximum:.12g} MWh，"
            f"所需平均利用率={utilization:.12g}，阈值={threshold:.12g}。"
            "不得降低目标占比或放宽容量。"
        )
    return {
        "E_S1_mwh": e_s1,
        "E_tasks_req_mwh": e_required,
        "E_tasks_max_mwh": e_maximum,
        "required_avg_utilization": utilization,
        "threshold": threshold,
        "feasible": True,
    }


def generate_tasks(
    it_base_mwh: float,
    horizon_hours: int,
    gpu_capacity_plan: int,
    flexible_ratio: float,
    seed: int,
    p_card_mw: float,
    s3_tolerance_hours: float = 12.0,
    gen_cfg: dict | None = None,
    priority_mix: dict[str, float] | None = None,
) -> pd.DataFrame:
    """生成算力任务表。

    ⚠️ **柔性占比用电量口径**（SPEC §2.2）：

        E_S1    = Σ_t IT_base(t)·Δt                     不可迁移负荷（含 S1）
        E_tasks = Σ_i gpu_i · p_card · dur_i            规划任务负荷（S2+S3）
        flexible_ratio = E_tasks / ( E_S1 + E_tasks )

    **分母必须含 S1**。若只对返回的任务表计算，分子分母都只剩 S2+S3，
    必然得到 1.0（100%），与画像设定（如 R-NW 的 0.65）无法比较——
    这正是 Y2 指出的口径错误。

    **必须由目标驱动生成**（不得反向操作）：

        1. 计算 E_S1（由 it_base_mwh 传入）
        2. **调用 check_flexible_ratio_feasible 做可达性预检**（§AA），
           不通过则直接报错
        3. 反解 E_tasks_target = E_S1 · R / (1 − R)
        4. 按 generation.yaml 的分布（含 by_priority 的分组分布）采样任务，
           累加电量直至达到目标
        5. 重算实际 flexible_ratio，与目标比较

    **禁止**先固定任务数、算出实际比例、再宣称"核验通过"。

    Args:
        it_base_mwh: 窗口内不可迁移 IT 电量 E_S1（MWh），由时间序列算出。
            **必需参数，不是可选项**——缺少它无法计算全系统分母。
        horizon_hours: 窗口长度（168 / 8760）
        gpu_capacity_plan: 规划可用卡数（= total − S1 reserved），不是 total
        flexible_ratio: 目标柔性占比 R
        seed: 随机种子
        p_card_mw: 单卡功率（MW/卡），由 system.yaml 的 gpu_power 换算
        s3_tolerance_hours: S3 容忍窗口，可被画像覆盖
        gen_cfg: configs/generation.yaml 的内容（GPU 取值、时长分布、松弛分布等）
        priority_mix: 可选的 S2/S3 数量概率覆盖，和须为 1；默认使用 gen_cfg。

    Returns:
        DataFrame，列为 TASK_COLUMNS，并在 `attrs` 中附带：

            attrs["E_S1_mwh"]              = it_base_mwh
            attrs["E_tasks_mwh"]           = 实际任务电量
            attrs["E_IT_mwh"]              = 两者之和
            attrs["flexible_ratio"]        = E_tasks / E_IT
            attrs["flexible_ratio_target"] = 输入的目标 R
            attrs["n_resample"]            = 因窗口越界重采样的次数

    ⚠️ 窗口边界硬要求（SPEC §3.1）：

        arrive_h + duration_h ≤ horizon_hours
        latest_h  = deadline_h + tolerance_h ≤ horizon_hours

    越界时应**重新采样该任务的到达时间与松弛**（往前放），
    **禁止**截断 duration、缩短 tolerance、提前 deadline 或丢弃任务。
    重采样次数用尽仍失败时应报错，不得静默放宽边界。
    """
    e_s1 = _finite_number(it_base_mwh, "it_base_mwh")
    horizon = _positive_integer(horizon_hours, "horizon_hours")
    capacity = _positive_integer(gpu_capacity_plan, "gpu_capacity_plan")
    p_card = _finite_number(p_card_mw, "p_card_mw", positive=True)
    s3_tolerance = _finite_number(s3_tolerance_hours, "s3_tolerance_hours")
    generation = _load_default_config("generation.yaml") if gen_cfg is None else gen_cfg
    task_config = generation["tasks"]
    constraints = generation["task_generation_constraints"]
    ratio_tolerance = _finite_number(
        constraints["flexible_ratio_tolerance"], "flexible_ratio_tolerance"
    )
    precheck = check_flexible_ratio_feasible(
        e_s1 / horizon,
        horizon,
        capacity,
        p_card,
        flexible_ratio,
        constraints["max_required_avg_utilization"],
    )
    target_ratio = float(flexible_ratio)
    target_energy = e_s1 * target_ratio / (1 - target_ratio)
    max_resample = _positive_integer(
        constraints["max_resample_attempts"], "max_resample_attempts"
    )
    if constraints["resample_on_violation"] is not True:
        raise ValueError("SPEC 要求 resample_on_violation=true，不允许放宽窗口边界")
    if constraints["fail_on_exhausted_resample"] is not True:
        raise ValueError("SPEC 要求 fail_on_exhausted_resample=true")

    priorities = ("S2", "S3")
    mix = task_config["priority_mix"] if priority_mix is None else priority_mix
    unsupported = set(mix) - set(priorities) - {"source", "note"}
    if unsupported:
        raise ValueError(f"priority_mix 只允许 S2/S3 概率，非法键：{sorted(unsupported)}")
    probabilities = np.asarray([mix[priority] for priority in priorities], dtype=float)
    if (
        not np.all(np.isfinite(probabilities))
        or np.any(probabilities < 0)
        or not np.isclose(probabilities.sum(), 1.0, rtol=1e-9, atol=1e-12)
    ):
        raise ValueError("priority_mix 的 S2/S3 概率必须非负且和为 1")
    priority_cdf = np.cumsum(probabilities / probabilities.sum())
    priority_cdf[-1] = 1.0

    intensity = np.asarray(task_config["arrival_intensity_by_hour"]["values"], dtype=float)
    if (
        intensity.shape != (24,)
        or not np.all(np.isfinite(intensity))
        or np.any(intensity < 0)
        or intensity.sum() <= 0
    ):
        raise ValueError("arrival_intensity_by_hour 必须包含 24 个非负、非全零有限值")
    arrival_weights = intensity[np.arange(horizon) % len(intensity)]
    if arrival_weights.sum() <= 0:
        raise ValueError("当前 horizon_hours 内没有正的任务到达强度")
    arrival_cdf = np.cumsum(arrival_weights / arrival_weights.sum())
    arrival_cdf[-1] = 1.0
    arrival_values = np.arange(horizon)

    distributions = {}
    for priority, probability in zip(priorities, probabilities, strict=True):
        config = task_config["by_priority"][priority]
        gpu_values, gpu_cdf = _distribution(config["gpu_choices"], f"{priority}.gpu_choices")
        durations, duration_cdf = _distribution(
            config["duration_distribution"], f"{priority}.duration_distribution"
        )
        if probability > 0 and np.any(gpu_values[gpu_cdf - np.r_[0, gpu_cdf[:-1]] > 0] > capacity):
            raise ValueError(f"{priority}.gpu_choices 存在超过 gpu_capacity_plan={capacity} 的任务")
        slack_config = config["deadline_slack_h"]
        if slack_config["distribution"] != "uniform":
            raise ValueError(f"不支持的 {priority}.deadline_slack_h 分布")
        slack_min = _finite_number(slack_config["min"], f"{priority}.slack.min")
        slack_max = _finite_number(slack_config["max"], f"{priority}.slack.max")
        slack_low, slack_high = int(np.ceil(slack_min)), int(np.floor(slack_max))
        if slack_min > slack_max or slack_low > slack_high:
            raise ValueError(f"{priority} 的松弛区间中没有有效整数小时")
        if priority == "S2":
            tolerance = _finite_number(config["tolerance_h"], "S2.tolerance_h")
            if tolerance != 0:
                raise ValueError("S2 为硬截止，tolerance_h 必须为 0")
        elif config["tolerance_h"] == "from_region":
            tolerance = s3_tolerance
        else:
            tolerance = _finite_number(config["tolerance_h"], "S3.tolerance_h")
        distributions[priority] = (
            gpu_values, gpu_cdf, durations, duration_cdf, slack_low, slack_high, tolerance
        )

    token_rate = _finite_number(
        _load_default_config("system.yaml")["compute"]["token_per_gpu_hour"],
        "system.compute.token_per_gpu_hour",
    )
    rng = np.random.default_rng(seed)
    rows = []
    energy = 0.0
    n_resample = 0
    while energy < target_energy:
        priority = priorities[int(np.searchsorted(priority_cdf, rng.random(), side="right"))]
        gpu_values, gpu_cdf, durations, duration_cdf, slack_low, slack_high, tolerance = (
            distributions[priority]
        )
        gpu = _draw_discrete(rng, gpu_values, gpu_cdf)
        duration = _draw_discrete(rng, durations, duration_cdf)
        for attempt in range(max_resample + 1):
            arrive = _draw_discrete(rng, arrival_values, arrival_cdf)
            slack = int(rng.integers(slack_low, slack_high + 1))
            deadline = arrive + duration + slack
            latest = deadline + tolerance
            if arrive + duration <= horizon and latest <= horizon:
                break
            if attempt == max_resample:
                raise ValueError(
                    f"任务窗口重采样耗尽：priority={priority}，gpu={gpu}，"
                    f"duration_h={duration}，tolerance_h={tolerance}，"
                    f"horizon_hours={horizon}，重采样上限={max_resample}。"
                    "未缩短任务、容忍窗口或截止余量，未丢弃任务。"
                )
            n_resample += 1
        rows.append((
            f"task_{len(rows):06d}", priority, gpu, duration, arrive, deadline,
            tolerance, latest, gpu * duration * token_rate,
        ))
        energy += gpu * duration * p_card

    tasks = pd.DataFrame.from_records(rows, columns=TASK_COLUMNS).astype({
        "task_id": "object", "priority": "object", "gpu": "int64",
        "duration_h": "int64", "arrive_h": "int64", "deadline_h": "int64",
        "tolerance_h": "float64", "latest_h": "float64", "tokens_mtok": "float64",
    })
    # 用最终表重算，避免逐任务浮点累加成为统计量的另一套口径。
    energy = float((tasks["gpu"] * tasks["duration_h"]).sum()) * p_card
    e_it = e_s1 + energy
    actual_ratio = energy / e_it if e_it > 0 else 0.0
    tasks.attrs.update({
        "E_S1_mwh": e_s1,
        "E_tasks_mwh": energy,
        "E_IT_mwh": e_it,
        "flexible_ratio": actual_ratio,
        "flexible_ratio_target": float(flexible_ratio),
        "E_tasks_target_mwh": target_energy,
        "n_resample": n_resample,
        "flexible_ratio_tolerance": ratio_tolerance,
        "feasibility_precheck": precheck,
        "precheck_note": constraints["precheck_note"],
    })
    if abs(actual_ratio - flexible_ratio) > ratio_tolerance:
        logger.warning(
            "柔性电量占比偏差超容差：actual=%s target=%s tolerance=%s；"
            "按配置采样完整任务时产生电量离散误差，保留实际值和原目标。",
            actual_ratio, flexible_ratio, ratio_tolerance,
        )
    return tasks


def summarize_tasks(
    tasks: pd.DataFrame,
    horizon_hours: int,
    it_base_mwh: float,
    p_card_mw: float,
) -> dict:
    """任务表统计摘要（SPEC §2.2）。

    返回值必须包含：

        E_S1_mwh                不可迁移 IT 电量（含 S1）
        E_tasks_mwh             规划任务电量（S2+S3）
        E_IT_mwh                全系统 IT 电量（= E_S1 + E_tasks）
        flexible_ratio          实际柔性占比 = E_tasks / E_IT
        flexible_ratio_target   目标值
        flexible_ratio_deviation 实际 − 目标（用于核验）
        n_by_priority           各级任务数量
        gpu_total_hours         计划任务的 GPU·h 总量
        tokens_mtok             计划任务的 Token 产量
        sla_penalty_basis_mtok  最坏情形（全部 S3 延迟）的惩罚基准量

    ⚠️ 校验规则：若 |flexible_ratio_deviation| > 0.05，需告警并说明原因。
    任务电量难以精确命中目标时，**应在报告中给出实际值，而不是调整目标值**。

    Args:
        tasks: generate_tasks 的返回（需含 attrs 中的电量信息；若缺失则用
            it_base_mwh 与 p_card_mw 重算，但应优先取 attrs 以避免口径漂移）。
            已有 E_tasks_mwh 必须与当前行重算值一致，否则报错，避免切片继承旧 attrs。
        horizon_hours: 窗口长度
        it_base_mwh: 窗口内不可迁移 IT 电量 E_S1
        p_card_mw: 单卡功率（MW/卡）
    """
    _positive_integer(horizon_hours, "horizon_hours")
    p_card = _finite_number(p_card_mw, "p_card_mw", positive=True)
    missing = {"priority", "gpu", "duration_h", "tokens_mtok"} - set(tasks.columns)
    if missing:
        raise ValueError(f"任务表缺少统计所需列：{sorted(missing)}")
    if not tasks["priority"].isin(["S2", "S3"]).all():
        raise ValueError("任务表只允许 S2/S3；S1 已包含在 E_S1 中")
    quantities = tasks[["gpu", "duration_h", "tokens_mtok"]].to_numpy(dtype=float)
    if not np.all(np.isfinite(quantities)) or np.any(quantities < 0):
        raise ValueError("任务统计要求 GPU、时长、Token 均为有限非负数")
    gpu_hours = float((tasks["gpu"] * tasks["duration_h"]).sum())
    e_s1 = _finite_number(tasks.attrs.get("E_S1_mwh", it_base_mwh), "E_S1_mwh")
    recomputed_energy = gpu_hours * p_card
    energy = _finite_number(tasks.attrs.get("E_tasks_mwh", recomputed_energy), "E_tasks_mwh")
    if "E_tasks_mwh" in tasks.attrs and not np.isclose(
        energy, recomputed_energy, rtol=1e-9, atol=1e-12
    ):
        raise ValueError(
            f"任务表 attrs 的 E_tasks_mwh={energy:.12g} 与当前行重算值 "
            f"{recomputed_energy:.12g} 不一致；任务表可能被切片或修改，"
            "或 p_card_mw 与生成时不同。请明确当前表的统计口径，未更改目标或 S1 分母。"
        )
    e_it = e_s1 + energy
    actual_ratio = energy / e_it if e_it > 0 else 0.0
    target = float(tasks.attrs.get("flexible_ratio_target", np.nan))
    deviation = actual_ratio - target
    counts = {priority: int((tasks["priority"] == priority).sum()) for priority in ("S2", "S3")}
    tokens = float(tasks["tokens_mtok"].sum())
    # §3.4：SLA 惩罚以迟到任务数量占比乘窗口 Token 总量，而非迟到任务自身 Token。
    penalty_basis = counts["S3"] / len(tasks) * tokens if len(tasks) else 0.0
    if np.isfinite(target):
        tolerance = tasks.attrs.get("flexible_ratio_tolerance")
        if tolerance is None:
            tolerance = _load_default_config("generation.yaml")[
                "task_generation_constraints"
            ]["flexible_ratio_tolerance"]
        tolerance = _finite_number(tolerance, "flexible_ratio_tolerance")
        if abs(deviation) > tolerance:
            logger.warning(
                "柔性电量占比偏差超容差：actual=%s target=%s deviation=%s；"
                "完整任务电量离散，或任务表/输入口径已改变；未调整目标值。",
                actual_ratio, target, deviation,
            )
    return {
        "E_S1_mwh": e_s1,
        "E_tasks_mwh": energy,
        "E_IT_mwh": e_it,
        "flexible_ratio": actual_ratio,
        "flexible_ratio_target": target,
        "flexible_ratio_deviation": deviation,
        "n_by_priority": counts,
        "gpu_total_hours": gpu_hours,
        "tokens_mtok": tokens,
        "sla_penalty_basis_mtok": penalty_basis,
        "precheck_note": tasks.attrs.get(
            "precheck_note", "通过可达性预检不等于任务集一定可调度；可调度性须在 M2 求解验证。"
        ),
    }
