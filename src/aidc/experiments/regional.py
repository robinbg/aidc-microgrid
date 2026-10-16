"""地区映射：四画像配置推荐表（SPEC §7）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def run_regional_mapping(
    config_dir: Path,
    output_dir: Path,
    regions: list[str] | None = None,
    strategies: list[str] | None = None,
    seed: int = 20260816,
) -> pd.DataFrame:
    """对四个地区画像分别求最优配置，输出推荐表。

    Args:
        regions: None 时取 ["R-NW", "R-WPV", "R-EAST", "R-SOUTH"]

    Returns:
        推荐表，列为：
        region, E_Na, P_Na, E_Li, P_Li, dominant_chemistry, best_strategy,
        LCOE, carbon_per_mtok, t_island_mean, t_island_worst, eens
        其中 dominant_chemistry ∈ {"Na-dominant", "Li-dominant", "mixed"}

    ⚠️ **验收要求（SPEC §7.3）**：推荐表中应当出现"锂电为主"或"混合"的结论
    （R-EAST、R-SOUTH 是预期的候选画像）。
    若四个画像全部给出"钠电为主"，说明数据生成或参数设置有偏，
    需检查气候与布置条件（outdoor_installation、T_min）是否真正生效。
    """
    raise NotImplementedError("M8: 实现地区映射")


def classify_dominant(
    E_Na: float, E_Li: float, P_Na: float, P_Li: float, threshold: float = 0.6
) -> str:
    """判定主导化学体系。

    规则建议：按能量容量占比判定，占比 > threshold 者为 dominant，
    否则为 "mixed"。阈值需固定并写入报告。

    判定应考虑能量与功率两侧：若能量以钠电为主但功率几乎全在锂电，
    更准确的表述是"钠电能量型 + 锂电功率型"，属于本方案主张的分层配置，
    不应简单标为 "Na-dominant"。建议增加 "Na-energy+Li-power" 类别。
    """
    raise NotImplementedError("M8: 实现主导体系判定")


def build_replication_template(mapping: pd.DataFrame) -> pd.DataFrame:
    """把映射结果整理成"可查表的复制模板"（proposal 第四节 4.3 那张表）。

    列：地区画像 | 资源特征 | 储能配置建议 | 调度重心 | 韧性重心
    另一列 reserved 供填写"数值由仿真填入"的具体指标。
    """
    raise NotImplementedError("M8: 实现复制模板")
