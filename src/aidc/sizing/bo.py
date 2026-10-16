"""外层容量配置：贝叶斯优化（SPEC §4）。

⚠️ 这是 **4–6 维的低维问题**。禁止引入高维 BO 方法（trust region / 随机嵌入等）。
标准 GP-BO 即可，并把精力放在多保真与带噪目标上。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class BOResult:
    """BO 运行结果。"""

    best_x: np.ndarray                    # 最优配置（物理单位）
    best_objectives: np.ndarray           # 对应目标向量
    pareto_x: np.ndarray                  # 非支配解集合
    pareto_objectives: np.ndarray
    history: pd.DataFrame                 # 逐次评估记录（用于 hypervolume 曲线）
    n_evaluations: int
    n_high_fidelity: int
    weights: np.ndarray                   # 本次使用的目标权重向量
    normalization: dict = field(default_factory=dict)   # 归一化范围，需回传以便复现
    extra: dict = field(default_factory=dict)

    # history 必须包含的列：
    #   iteration, E_Na, P_Na, E_Li, P_Li, fidelity, LCOE, carbon_per_mtok,
    #   resilience_gap, sla_rate, hypervolume, status


def run_bo(
    region: str,
    budget: int,
    fidelity: str,
    strategy: str,
    weights: np.ndarray | None = None,
    seed: int = 20260816,
    config_dir=None,
) -> BOResult:
    """运行贝叶斯优化求容量配置。

    Args:
        region: 地区画像 ID
        budget: 评估预算（次）
        fidelity: "low" | "high"
        strategy: 策略 ID
        weights: 目标权重向量（长度 4）。None 时按标量化配置生成。
        seed: 随机种子
        config_dir: configs 目录

    Implementation notes:
        - backend 默认 botorch；若环境无法安装 torch，退化为
          scikit-optimize 或 sklearn 的 GaussianProcessRegressor + EI 手写循环。
          选择需记入日志。
        - 采集函数默认 EI；多目标可选 EHVI/qEHVI，或先用随机单纯形标量化。
        - **多保真流程**：先以低保真跑满 budget_low_fidelity，
          取 Top-K 候选（K = budget_high_fidelity）用高保真重新评估，
          以高保真结果决定最终推荐配置。K 与筛选准则需记录。
        - **带噪目标**：GP 需开启 noise 建模（alpha 或 WhiteKernel）。
          韧性指标依赖场景采样，同一配置在不同场景集下得分不同，
          因此必须噪声感知，否则会过拟合到随机场景。
        - 初始点：拉丁超立方或 Sobol 采样 initial_points 个点。
        - 每步都计算 hypervolume（用于与网格扫描对比，SPEC §4.4）。
    """
    raise NotImplementedError("M5: 实现 BO")


def compute_hypervolume(
    objectives: np.ndarray, reference_point: np.ndarray
) -> float:
    """计算超体积。

    ⚠️ reference_point（参考点）的取值必须固定并记录在报告中，
    否则超体积数值不可比。建议取各目标的"最差值 × 1.1"。
    """
    raise NotImplementedError("M5: 实现超体积计算")


def filter_pareto(objectives: np.ndarray) -> np.ndarray:
    """返回非支配解的行索引（全部目标取最小化）。"""
    raise NotImplementedError("M5: 实现非支配过滤")
