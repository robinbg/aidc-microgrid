"""网格扫描：BO 的对照组（SPEC §4.4）。

必须实现，且必须与 BO 使用**完全相同**的数据、目标函数与保真度设置，
否则对比无意义。

⚠️ 不要把 BO 的优越性写成前提。如果网格扫描在相同预算下更好，
如实报告，并说明 BO 的价值在于更少评估次数（适用于高保真评估昂贵时）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def build_grid(
    bounds: dict[str, tuple[float, float]],
    points_per_dim: int = 10,
    dimensions: list[str] | None = None,
) -> np.ndarray:
    """构造网格点。

    Args:
        bounds: {"E_Na": (0, 200), "P_Na": (0, 50), "E_Li": (0, 200), "P_Li": (0, 50)}
        points_per_dim: 每维点数，默认 10
        dimensions: 参与网格的维度；None 表示全部。臂约束下未参与的维度固定为 0。

    Returns:
        形状 (n_points, n_dims) 的数组，物理单位。

    Notes:
        4 维 × 每维 10 点 = 10⁴ 个组合。若算力不足，可先用 6 点/维（1296 组合）跑通，
        再补全 10 点/维。降采样时必须记录，并在报告中说明。
    """
    raise NotImplementedError("M5: 实现网格构造")


def run_grid_search(
    region: str,
    arm: str,
    strategy: str,
    fidelity: str = "low",
    points_per_dim: int = 10,
    seed: int = 20260816,
    config_dir=None,
) -> pd.DataFrame:
    """穷举网格并评估。

    Args:
        arm: 配置臂 ID，决定哪些维度参与网格（见 experiments.yaml）

    Returns:
        每行一个网格点的评估结果，列与 BOResult.history 对齐，
        便于与 BO 画在同一张 hypervolume 图上。

    Implementation notes:
        - **必须支持并行**（多进程），10⁴ 次内层 LOW 评估串行会很慢。
          并行时注意：每个进程独立设置随机种子，且结果需按点索引确定性归位。
        - 必须输出 hypervolume 随评估次数（按某种固定顺序累加）的曲线，
          与 BO 对比。累加顺序需固定（建议随机顺序 + 固定种子）。
    """
    raise NotImplementedError("M5: 实现网格扫描")


def compare_bo_vs_grid(
    bo_result, grid_result: pd.DataFrame
) -> dict:
    """BO 与网格扫描的对比。

    Returns:
        {
          "hv_curve_bo": pd.DataFrame,
          "hv_curve_grid": pd.DataFrame,
          "best_bo": float,
          "best_grid": float,
          "n_eval_bo": int,
          "n_eval_grid": int,
          "conclusion": str,        # 诚实结论，含"网格扫描更好"这种情形
        }

    ⚠️ conclusion 字段必须如实填写。若 BO 在相同评估次数下未优于网格扫描，
    直接写明，并说明在什么预算区间（评估昂贵时）BO 才划算。
    """
    raise NotImplementedError("M5: 实现对比")
