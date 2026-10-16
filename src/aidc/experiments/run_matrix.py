"""主实验矩阵：4 臂 × 5 策略 × 5 种子（SPEC §6.3）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


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
    """
    raise NotImplementedError("M4: 实现主实验矩阵")


def load_matrix_results(output_dir: Path) -> pd.DataFrame:
    """加载已有结果，便于断点续跑与事后分析。"""
    raise NotImplementedError("M4: 实现结果加载")
