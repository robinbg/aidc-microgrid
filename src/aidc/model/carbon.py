"""碳核算（SPEC §2.5）。

⚠️ 红线（SPEC §10.5）：
    碳减排量可以报告，但**不得**按碳价折算成收入计入经济性指标。
    如需给出参考估算，单列为"参考估算（未计入经济性）"并注明。
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "carbon_grass", "carbon_avoided_by_heat", "carbon_net", "carbon_per_mtok",
    "lifecycle_carbon_storage",
]


def _nonnegative(value, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if not np.all(np.isfinite(array)) or np.any(array < 0):
        raise ValueError(f"{name} 必须是有限非负值")
    return array


def _positive_scalar(value: float, name: str) -> float:
    result = float(value)
    if not np.isfinite(result) or result <= 0:
        raise ValueError(f"{name} 必须是有限正数")
    return result


def carbon_grass(
    g_imp: np.ndarray,
    CI_grid: np.ndarray,
    Q_boiler: np.ndarray | None = None,
    ci_boiler: float = 220.0,
    dt: float = 1.0,
) -> np.ndarray:
    """毛碳排（kgCO₂，SPEC §2.5）。

        carbon_grass = g_imp · Δt · CI_grid  +  Q_boiler · Δt · ci_boiler

    `ci_boiler` 应取自 `economics.yaml` 的 `heat_carbon.ci_boiler`
    （唯一真源，不在 system.yaml 重复定义）；默认值仅为无参调用时的回退。

    ⚠️ **锅炉项是 v1.7 新增的**。锅炉在系统边界内（scope 1），其碳排必须计入。

    ⚠️ **市政侧的余热收益不再作为 carbon_avoid 项**，而是体现为
    `Q_boiler` 降低 → 本函数值降低。理由：锅炉在系统边界内，
    用它自己减排更干净，且避免了"既算避免又算自身排放"的双重计算。

    工业供热的减排在边界外（替代了外部热源），保留在 carbon_avoid 中。
    """
    step = _positive_scalar(dt, "dt")
    grid = _nonnegative(g_imp, "g_imp")
    intensity = _nonnegative(CI_grid, "CI_grid")
    boiler = 0.0 if Q_boiler is None else _nonnegative(Q_boiler, "Q_boiler")
    boiler_intensity = _nonnegative(ci_boiler, "ci_boiler")
    return grid * step * intensity + boiler * step * boiler_intensity


def carbon_avoided_by_heat(
    Q_ind: np.ndarray, ci_substituted: float, dt: float = 1.0
) -> np.ndarray:
    """工业供热替代避免的碳排（kgCO₂）。

    ⚠️ v1.7 起**只计工业供热**（`Q_ind`），不再含市政（`Q_mun`）——
    市政侧的减排通过锅炉出力降低体现，见 carbon_grass。

    口径：Q_ind（MW_th）· Δt（h）= MWh_th，乘以被替代热源的碳强度。

    ⚠️ ci_substituted 的取值直接决定该指标大小，必须标注所替代的热源类型
    并在报告中说明这是估算口径，非第三方核证结果。
    """
    return (_nonnegative(Q_ind, "Q_ind") * _positive_scalar(dt, "dt")
            * _nonnegative(ci_substituted, "ci_substituted"))


def carbon_net(
    g_imp: np.ndarray,
    CI_grid: np.ndarray,
    Q_ind: np.ndarray,
    ci_substituted: float,
    Q_boiler: np.ndarray | None = None,
    ci_boiler: float = 220.0,
    dt: float = 1.0,
) -> np.ndarray:
    """净碳排 = 毛碳排 − 工业供热替代避免碳排。

    允许净碳排为负（当绿电与工业供热替代足够多时），但报告中需说明这一情形的含义，
    避免被误读为"负排放设施"。
    """
    return carbon_grass(
        g_imp, CI_grid, Q_boiler, ci_boiler, dt
    ) - carbon_avoided_by_heat(Q_ind, ci_substituted, dt)


def carbon_per_mtok(carbon_net_total_kg: float, tokens_mtok: float) -> float:
    """单位 Token 净碳排（kgCO₂/MTok）。

    ⚠️ 若 tokens_mtok 为 0，需返回 NaN 而非 0，否则会污染聚合统计。
    """
    carbon = float(carbon_net_total_kg)
    tokens = float(tokens_mtok)
    if not np.isfinite(carbon) or not np.isfinite(tokens) or tokens < 0:
        raise ValueError("净碳排必须有限，Token 产量必须有限且非负")
    return carbon / tokens if tokens > 0 else float("nan")


def lifecycle_carbon_storage(
    E_Na: float, E_Li: float, mfg_na: float, mfg_li: float, years: float
) -> float:
    """储能全生命周期碳足迹（kgCO₂/年）。

    制造阶段碳足迹按项目周期摊销：

        ( E_Na · mfg_na + E_Li · mfg_li ) / years

    ⚠️ **`mfg_na` / `mfg_li` 是内部单位 kgCO₂/MWh，公式内不得再乘 1000**
    （v2.2 修正）。加载层已把配置的 `manufacturing_kgco2_per_kwh`
    （kgCO₂/kWh）换算为 `manufacturing_kgco2_per_mwh`（kgCO₂/MWh），
    而 E 的单位是 MWh，两者相乘直接得 kgCO₂。

    早期 docstring 写"mfg 单位为 kgCO₂/kWh、需乘 1000"——那是**双重换算**，
    会把制造碳足迹虚高 1000 倍（与 area_requirement 的同类错误，见 SPEC §2.3）。

    该值是否计入 carbon_per_mtok 由 `carbon.include_manufacturing_in_net`
    决定（默认 false）；**报告中必须写明当前口径**。
    """
    parameters = _nonnegative([E_Na, E_Li, mfg_na, mfg_li], "容量与制造碳强度")
    energy_na, energy_li, intensity_na, intensity_li = parameters
    return float((energy_na * intensity_na + energy_li * intensity_li)
                 / _positive_scalar(years, "years"))
