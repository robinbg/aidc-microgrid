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

    M4 实现：CSV 为全列原样（100 组每组一行）；Markdown 附口径注记
    （f3 不可分辨、绿电匹配率口径、S-base 政策性弃电、BT2 临时结论标记、
    规模归一化说明、路径选择），并给出 臂×策略 的跨种子聚合视图便于阅读。
    不做任何跨臂"获胜者"排序——§9.37 禁止预设结果，比较须携带投资额注记。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "master_table.csv"
    md_path = output_dir / "master_table.md"
    matrix_results.to_csv(csv_path, index=False)

    notes = [
        "**路径**：低保真代表周（5×168 h，权重 0.18/0.16/0.22/0.22/0.22，覆盖四季 + 极端低温周 W-WINX；"
        "BT2 裁定，§6.3 要求写明路径选择）。年化 = Σ 周结果 × 权重 × 365/7。",
        "**f3 / 韧性列不可分辨（NON-DISCRIMINATING）**：低保真 `resilience_gap = NaN`（§4.2）；"
        "臂级 T_island/EENS/N-1 列来自 M2.5 场景包（故障调度与策略无关，按臂合并），"
        "当前占位数据下孤岛观测**全部右删失**，这些列**不得参与任何排序**，仅作口径披露。",
        "**绿电匹配率口径**：分母含储能充电、不追溯储能出力来源（solver_info 已声明），"
        "**不可跨储能规模直接对比**（A0 与带储能臂的差异主要是口径效应）。",
        "**S-base 注记**：规则驱动（objective 为 NaN 属契约行为）；其盈余/电网充电只到 soc_ref"
        "（BS3 策略约束），弃电随储能容量放大是**内建不对称**，跨策略比较须携带此注记；"
        "S-base 的目标值不与其他策略直接比较（量纲不同）。",
        "**规模归一化**：A1/A2/A3 统一 150 MWh / 45 MW（§6.1），投资额差异只反映化学体系单价；"
        "A0 投资为 0。跨臂比较 LCOE 时必须同时给出 `investment_yuan` 与 `net_annual_cost`。",
        "**BT2-③ 临时结论标记**：`provisional_for_comparison = True` 表示该组存在认证 gap > 2% 的窗口，"
        "涉及该组的跨臂对比只能作为临时结论。报告的 gap 一律为**认证 gap**（后端对偶界或夹逼证书），"
        "不是可行解自述值。",
        "**容量衰减列为估算值**（1 年运行口径，§10.1），不进成本侧；零容量体系记 NaN。",
        "**钠电不预设优势**：本表不给出任何「获胜配置」；结论表述一律为"
        "「在参数区间 P 下，配置 C 在指标 M 上更优/更差」。",
    ]
    definitions = [
        "**SLA 达标率定义**：1 − 延迟任务数 / 总任务数；分母只含 S2/S3 规划任务"
        "（S1 在线推理已并入 IT_base，不重复计算）；延迟 = 完成时刻 > deadline_h。",
        "**关键负荷定义（韧性评估用）**：critical_it_fraction = 0.60 × IT_base（设施侧 IT 口径，"
        "母线侧 × PUE）；判据 shed_c ≤ ε_shed = 1e-9 MW。",
        "**降级策略定义（韧性评估用）**：故障模式四级字典序（关键削减 → 非关键削减 → 供热缺口 → "
        "化学体系偏好），第一级 gap = 0 求至证明最优；T_island 是**降级策略下**的续航，"
        "不是最优策略下的续航。",
        "**等效循环次数口径**：Σ_t max(0, soc(t) − soc(t+1))（SOC 掉幅口径，不除以 2）。",
        "**LCOE 口径**：(年化运营成本 + 储能年化成本) / 年服务电量 Σ IT_load·Δt；运营成本含购电 − 售电 "
        "− 工业供热收入 + 锅炉燃料成本 + 弃电惩罚，**不含 SLA 惩罚**；储能年化成本含 CRF×投资 "
        "+ 运维 + **替换成本**（CRF 由折现率 0.06 与项目寿命 20 年现算）。",
        "**碳账口径**：电网因子（区域常数，生成数据无时变幅度）+ 锅炉燃气 − 工业供热替代；"
        "储能制造碳按项目寿命摊销计入净碳；**碳资产不折算收入计入经济性指标**。",
    ]

    lines = ["# 主实验对照表（M4：4 臂 × 5 策略 × 5 种子）", "",
             f"> {DISCLAIMER}", "", "## 报告口径（SPEC §10.6 固定定义）", ""]
    lines += definitions + ["", "## 阅读注记（比较前必读）", ""]
    lines += [f"- {note}" for note in notes]

    def fmt(value, spec):
        if value is None or (isinstance(value, float) and not pd.notna(value)):
            return "—"
        return format(value, spec)

    aggregate_cols = {
        "LCOE": ("LCOE 元/MWh", ".1f"), "net_annual_cost": ("净年成本 万元", ".1f"),
        "carbon_per_mtok": ("碳 kgCO₂/MTok", ".5f"), "sla_rate": ("SLA 达标率", ".6f"),
        "curtailment_rate": ("弃电率", ".4f"), "green_matching_rate": ("绿电匹配率*", ".4f"),
        "equivalent_cycles_Na": ("循环 Na", ".1f"), "equivalent_cycles_Li": ("循环 Li", ".1f"),
        "heat_from_waste_share": ("余热占市政供热", ".4f"),
    }
    lines += ["", "## 臂 × 策略聚合视图（跨 5 个种子：均值 ± 标准差）", "",
              "| 臂 | 策略 | 投资额 万元 | " + " | ".join(h for h, _ in aggregate_cols.values())
              + " | 状态 | 临时标记 |",
              "|---|---|---:|" + "---:|" * len(aggregate_cols) + "---|---|"]
    for (arm, strategy), group in matrix_results.groupby(["arm", "strategy"], sort=True):
        cells = []
        for key in aggregate_cols:
            spec = aggregate_cols[key][1]
            values = pd.to_numeric(group[key], errors="coerce") if key in group else pd.Series(dtype=float)
            if key == "net_annual_cost":
                values = values / 1e4
            mean, std = values.mean(skipna=True), values.std(skipna=True)
            cells.append("—" if pd.isna(mean) else
                         (fmt(mean, spec) if pd.isna(std) else f"{fmt(mean, spec)} ± {fmt(std, spec)}"))
        invest = pd.to_numeric(group["investment_yuan"], errors="coerce").iloc[0] / 1e4
        statuses = "/".join(sorted(set(group["status"].astype(str))))
        provisional = "是" if bool(group.get("provisional_for_comparison", pd.Series([False])).any()) else "否"
        lines.append(f"| {arm} | {strategy} | {fmt(invest, '.1f')} | " + " | ".join(cells)
                     + f" | {statuses} | {provisional} |")
    lines += ["", "*绿电匹配率不可跨储能规模直接对比（口径见上）。", "",
              "## 全量明细", "",
              f"100 组（或子集）逐行明细见 `{csv_path.name}`（全列 CSV）。", ""]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path


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
