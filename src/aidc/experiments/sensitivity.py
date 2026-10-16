"""敏感性分析：三组扫描（SPEC §6.5）。

⚠️ 每一组都必须输出"结论成立边界"，即明确写出：
    在什么参数范围内本方案的配置主张成立，在什么条件下翻转。
只给趋势图而不给边界视为未完成。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def sensitivity_na_params(
    region: str, config_dir: Path, output_dir: Path, seed: int = 20260816
) -> dict:
    """S1 · 钠电参数区间扫描。

    扫描对象：
        - chemistry_na.cost.unit_cost_energy（low / mid / high）
        - chemistry_na.temp_derating.gamma（曲线整体缩放或取 low/high 版本）
        - chemistry_na.lifetime.cycle_life（low / mid / high）

    Returns:
        {
          "table": pd.DataFrame,          # 每个组合的指标
          "boundary": dict,               # 结论成立边界
          "flip_points": list[dict],      # 结论发生翻转的参数区间
          "figure_path": Path,
        }

    Implementation notes:
        - 建议用正交/拉丁超立方采样，不要全组合枚举（3³ = 27 尚可，
          若再增加维度则需采样）。
        - **必须验证参数真的生效**（SPEC §9.11）：检查同一配置在不同参数下
          结果确有差异。若全无差异，说明参数未进入模型，是 bug。
    """
    raise NotImplementedError("M7: 实现 S1 扫描")


def sensitivity_li_price(
    region: str,
    config_dir: Path,
    output_dir: Path,
    multipliers: list[float] | None = None,
    seed: int = 20260816,
) -> dict:
    """S2 · 锂价情景扫描。

    对锂电 unit_cost_energy 乘上倍率（默认 [0.6, 1.0, 1.6]），
    产出"在什么价格组合下钠电臂在 LCOE 上胜出"的判据。

    这是本方案主动承认"当前钠电不比 LFP 便宜"后必须给出的对应分析，
    结论需写成判据形式（例如"当锂价高于 X 元/kWh 且钠电低于 Y 元/kWh 时，
    钠电臂的 LCOE 更优"）。
    """
    raise NotImplementedError("M7: 实现 S2 扫描")


def sensitivity_region_compute(
    region: str, config_dir: Path, output_dir: Path, seed: int = 20260816
) -> dict:
    """S3 · 地区与算力条件扫描。

    扫描：风光容量因子（cf_pv / cf_wind）、任务柔性比（flexible_task_ratio）。

    柔性比是本题的关键变量：它决定"算力虚拟储能"能顶掉多少物理储能，
    因此预期会显著改变最优配置。若扫描发现柔性比对配置无影响，
    需检查任务模型是否真的实现了可迁移约束。
    """
    raise NotImplementedError("M7: 实现 S3 扫描")


def summarize_boundaries(results: dict[str, dict]) -> pd.DataFrame:
    """汇总三组扫描的结论边界，输出为报告可直接引用的表。"""
    raise NotImplementedError("M7: 实现边界汇总")
