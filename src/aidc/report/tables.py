"""结果导出：表格与图表（SPEC §6.4、§12）。

⚠️ 表述红线（SPEC §10），所有导出内容必须遵守：
    1. 必须标注"仿真结果"，禁止出现"实测""真实运行数据""现场验证"
    2. 余热表述为低品位热源 + 热泵提温后的补充热源，不得写为主要热源
    3. 不做收益承诺
    4. 钠电不预设优势，结论写成"在参数区间 P 下，配置 C 在指标 M 上更优"
    5. 碳资产不折算收入计入经济性
    6. SLA 定义、关键负荷定义、降级策略定义必须在报告开头固定
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

DISCLAIMER = (
    "全部结果为确定性仿真结果，非真实设备测量数据。"
    "参数取自公开文献与行业公开区间的占位取值，待标定后更新。"
)

# 报告开头必须出现的定义，防止关键数字失去解释（SPEC §10.6）
REQUIRED_DEFINITIONS = [
    "SLA 达标率定义",
    "关键负荷定义（韧性评估用）",
    "降级策略定义（韧性评估用）",
    "等效循环次数口径",
    "LCOE 口径（是否含储能年化成本与替换成本）",
    "碳账口径（碳因子来源、替代热源类型、碳资产是否计入经济性）",
]


def export_master_table(
    matrix_results: pd.DataFrame, output_dir: Path
) -> tuple[Path, Path]:
    """导出主实验对照表（CSV + Markdown）。

    Markdown 版本需在开头附 DISCLAIMER 与 REQUIRED_DEFINITIONS。
    """
    raise NotImplementedError("M8: 实现主表导出")


def export_regional_table(mapping: pd.DataFrame, output_dir: Path) -> Path:
    """导出地区配置推荐表（proposal 第四节那张表的实际数值版）。"""
    raise NotImplementedError("M8: 实现地区表导出")


def export_sensitivity_boundaries(boundaries: pd.DataFrame, output_dir: Path) -> Path:
    """导出三组敏感性分析的结论边界。"""
    raise NotImplementedError("M8: 实现边界导出")


# ---------------------------------------------------------------- 图表（SPEC §12）

def plot_storage_derating_comparison(
    na_params, li_params, T_range: tuple[float, float], output_dir: Path
) -> Path:
    """图 1：钠电与锂电的低温可用容量折减曲线对比。

    这张图是"低温折减"这一机制的入口说明，横轴温度、纵轴 gamma(T)。
    ⚠️ 必须在图注中标注两条曲线的数据来源；若仍为占位值，明确标注。
    """
    raise NotImplementedError("M8: 实现折减对比图")


def plot_dispatch_trajectories(
    results: dict[str, pd.DataFrame], window_hours: int, output_dir: Path
) -> Path:
    """图 2：四配置臂的逐时段对比（核心演示图，SPEC §12）。

    输入为 {arm_id: trajectory_df}，同一份气象与任务数据、同一时间窗口。
    建议 2×2 子图：储能可用容量曲线、风电消纳与购电量、净碳排累计、SOC 轨迹。

    ⚠️ 这是最终交付的标志性图。**变量只有一个（储能配置）**，
    其他输入必须完全相同，否则图的说服力会失效。图注需写明这一点。
    """
    raise NotImplementedError("M8: 实现调度轨迹对比图")


def plot_pareto_front(
    pareto_by_arm: dict[str, pd.DataFrame], output_dir: Path
) -> Path:
    """图 3：四配置臂的 Pareto 前沿对比（消纳量 vs 碳排或 LCOE）。"""
    raise NotImplementedError("M8: 实现 Pareto 图")


def plot_bo_vs_grid(
    hv_curves: dict[str, pd.DataFrame], output_dir: Path
) -> Path:
    """图 4：BO 与网格扫描的超体积曲线对比（SPEC §4.4）。

    ⚠️ 这张图可能显示网格扫描更优，必须如实绘制，不得只画有利区间。
    """
    raise NotImplementedError("M8: 实现 BO 对比图")


def plot_resilience(
    resilience_results: dict[str, dict], output_dir: Path
) -> Path:
    """图 5：故障场景下的 SOC 轨迹与孤岛续航对比。

    需在图注中说明使用降级策略，否则 T_island 的含义会被误读（SPEC §5.4）。
    """
    raise NotImplementedError("M8: 实现韧性图")
