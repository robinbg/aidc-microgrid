"""电化学参数加载与温度相关特性。

本模块是 SPEC §2.3 的实现基础。所有低温相关特性都在这里集中定义，
供内层调度（SOC 上下界、功率上限）与外层目标（可用容量）共同调用。

设计要点（见 SPEC §1.3）：
    - 参数一律以 {low, mid, high} 三元组给出，默认用 mid。
    - 本模块不内置任何"钠电优于锂电"的假设。两条曲线的差异必须来自 config。
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

import numpy as np
import yaml

Triple = dict[str, float]  # {"low": ..., "mid": ..., "high": ...}
Scenario = Literal["low", "mid", "high"]

__all__ = [
    "ChemistryParams", "ConfigError", "Curve", "Scenario", "Triple",
    "apply_lithium_price_scenario", "convert_units", "load_chemistry",
    "chemistry_params_from_config",
    "load_config_bundle", "load_yaml", "storage_temperature",
    "validate_registry", "validate_unit_metadata",
]

logger = logging.getLogger(__name__)
_PLACEHOLDER_WARNED: set[Path] = set()
_SCENARIOS = ("low", "mid", "high")
# SPEC §1.1 的技术换算表；业务参数仍全部来自配置。
_UNIT_CONVERSIONS = {
    "元/kWh": ("元/MWh", 1000.0),
    "元/kW": ("元/MW", 1000.0),
    "元/kWh/年": ("元/MWh/年", 1000.0),
    "m²/kWh": ("m²/MWh", 1000.0),
    "kgCO₂/kWh": ("kgCO₂/MWh", 1000.0),
    "kW/卡": ("MW/卡", 0.001),
}
_UNIT_FIELDS = {
    "unit_cost_energy": ("元/kWh", "元/MWh"),
    "unit_cost_power": ("元/kW", "元/MW"),
    "opex_annual": ("元/kWh/年", "元/MWh/年"),
    "area_per_kwh": ("m²/kWh", "m²/MWh"),
    "manufacturing_kgco2_per_kwh": ("kgCO₂/kWh", "kgCO₂/MWh"),
    "gpu_power": ("kW/卡", "MW/卡"),
}
_DOCUMENT_KEYS = {
    "source", "note", "description", "display_name", "history", "precheck_note",
    "tolerance_note", "ci_boiler_source", "ci_ind_substituted_type",
}


class ConfigError(ValueError):
    """配置结构、单位或参数身份不满足 SPEC。"""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader 加上同一 YAML 映射内的重复键检测。"""


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep=False) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConfigError("YAML 配置键必须可哈希") from exc
        if duplicate:
            raise ConfigError(f"YAML 重复键 {key!r}（第 {key_node.start_mark.line + 1} 行）")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def load_yaml(path: Path | str) -> dict:
    """安全读取 YAML 映射；重复键、非映射根节点直接报错。"""
    path = Path(path)
    try:
        data = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: YAML 根节点必须是映射")
    return data


def _walk_nodes(tree: Any, prefix: str = "", include_lists: bool = False):
    """遍历真实配置键；列表作为叶子，避免将声明里的路径当成参数定义。"""
    if isinstance(tree, Mapping):
        for key, value in tree.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, value
            if isinstance(value, Mapping):
                yield from _walk_nodes(value, path, include_lists)
            elif include_lists and isinstance(value, list):
                for index, child in enumerate(value):
                    yield from _walk_nodes(child, f"{path}[{index}]", include_lists)


def validate_unit_metadata(tree: Mapping, prefix: str = "") -> None:
    """检查需换算的参数元数据以及不一致的单位后缀（SPEC §1.1）。"""
    for path, entry in _walk_nodes(tree, prefix, include_lists=True):
        name = path.rsplit(".", 1)[-1]
        expected = _UNIT_FIELDS.get(name)
        if expected is not None:
            if not isinstance(entry, Mapping):
                raise ConfigError(f"{path}: 必须用带 unit/internal_unit 的参数块")
            source, internal = expected
            if entry.get("internal_unit") != internal or entry.get("unit") not in (source, internal):
                raise ConfigError(f"{path}: unit/internal_unit 必须表达 {source} → {internal}")
        if not isinstance(entry, Mapping):
            continue
        unit, internal = entry.get("unit"), entry.get("internal_unit")
        if unit in _UNIT_CONVERSIONS:
            required, _ = _UNIT_CONVERSIONS[unit]
            if internal != required:
                raise ConfigError(f"{path}: {unit} 必须声明 internal_unit={required}")
        elif internal is not None and unit != internal:
            raise ConfigError(f"{path}: 未知或不一致的单位换算 {unit!r} → {internal!r}")
        if unit is not None:
            suffix_units = {
                "_mw_th": "MW_th", "_mwh_th": "MWh_th",
                "_mw": "MW", "_mwh": "MWh", "_celsius": "℃", "_m2": "m²",
            }
            for suffix, suffix_unit in suffix_units.items():
                if name.endswith(suffix) and unit != suffix_unit:
                    raise ConfigError(f"{path}: 键名单位与 unit={unit!r} 不一致")


def convert_units(tree: Any) -> Any:
    """返回内部单位的深复制；仅转换 value/low/mid/high，并更新 unit 防止重复换算。

    原字段名、source、internal_unit 等元数据保留。曲线 points 已是内部单位，
    不缩放断点、scale_range 或无关数值。同一个返回值再次转换不会重复乘因子。
    """
    if isinstance(tree, list):
        return [convert_units(value) for value in tree]
    if not isinstance(tree, Mapping):
        return copy.deepcopy(tree)
    result = {key: convert_units(value) for key, value in tree.items()}
    unit, internal = result.get("unit"), result.get("internal_unit")
    if unit in _UNIT_CONVERSIONS:
        required, factor = _UNIT_CONVERSIONS[unit]
        if internal != required:
            raise ConfigError(f"{unit}: 缺少或错误的 internal_unit（期望 {required}）")
        numeric_keys = [key for key in ("value", "low", "mid", "high") if key in result]
        if not numeric_keys:
            raise ConfigError(f"{unit}: 待换算参数必须包含 value 或 low/mid/high")
        for key in numeric_keys:
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
                raise ConfigError(f"{unit}.{key}: 必须为有限数值")
            result[key] = value * factor
        result["unit"] = internal
    elif internal is not None and unit != internal:
        raise ConfigError(f"未知单位换算 {unit!r} → {internal!r}")
    return result


def _namespace_configs(bundle: Mapping) -> list[tuple[str, Mapping]]:
    registry = bundle["registry"]
    items = []
    for filename, namespace in registry["namespaces"].items():
        if filename == "note":
            continue
        if "*" in filename:
            if filename != "regions/*.yaml":
                raise ConfigError(f"未支持的配置文件角色 {filename}")
            for region_id, region in bundle["regions"].items():
                items.append((namespace.replace("<画像ID>", region_id), region))
        else:
            key = Path(filename).stem
            if key not in bundle:
                raise ConfigError(f"登记文件不存在: {filename}")
            items.append((namespace, bundle[key]))
    return items


def validate_registry(bundle: Mapping) -> None:
    """执行命名空间、地区授权与规范量引用闭包/环检测；不按裸参数名判重。"""
    registry = bundle["registry"]
    namespaced: dict[str, Any] = {}
    business_paths = set()
    for namespace, config in _namespace_configs(bundle):
        for path, value in _walk_nodes(config):
            full_path = f"{namespace}.{path}"
            if full_path in namespaced:
                raise ConfigError(f"命名空间内重复定义: {full_path}")
            namespaced[full_path] = value
            if namespace != "registry":
                business_paths.add(full_path)

    authorized_overrides = registry["region_override_authorized"]
    exclusive_regions = registry["region_exclusive"]
    used_region_paths = set()
    for region_id, region in bundle["regions"].items():
        for path, value in _walk_nodes(region):
            if isinstance(value, Mapping):
                continue
            if path.rsplit(".", 1)[-1] in _DOCUMENT_KEYS:
                continue
            used_region_paths.add(path)
            # 直接使用完整默认命名空间也不允许绕过地区授权。
            if any(fnmatchcase(path, pattern) or fnmatchcase(f"region.{region_id}.{path}", pattern)
                   for pattern in registry["default_exclusive"]):
                raise ConfigError(f"region.{region_id}.{path}: 默认独占参数不得按画像覆盖")
            if not any(fnmatchcase(path, pattern) for pattern in authorized_overrides + exclusive_regions):
                raise ConfigError(f"region.{region_id}.{path}: 地区覆盖未授权")
    unused = [pattern for pattern in authorized_overrides
              if not any(fnmatchcase(path, pattern) for path in used_region_paths)]
    if unused:
        logger.warning("未在画像使用的覆盖授权项: %s", ", ".join(unused))

    truths = registry["canonical_config_truths"]
    derived_items = registry["derived_quantities"]
    locations = [item["authorized"] for item in truths]
    truth_ids = [item["quantity_id"] for item in truths]
    derived_ids = [item["quantity_id"] for item in derived_items]
    if len(locations) != len(set(locations)):
        raise ConfigError("canonical_config_truths.authorized 位置必须两两不同")
    if len(truth_ids + derived_ids) != len(set(truth_ids + derived_ids)):
        raise ConfigError("quantity_id 必须唯一")
    for location in locations:
        if location not in business_paths:
            raise ConfigError(f"authorized 位置不存在: {location}")
    dependencies = {}
    for item in derived_items:
        quantity_id = item["quantity_id"]
        if "authorized" in item or quantity_id in locations:
            raise ConfigError(f"派生量 {quantity_id} 不得有 authorized")
        refs = item["derived_from"]
        refs = [refs] if isinstance(refs, str) else refs
        if not isinstance(refs, list) or not refs:
            raise ConfigError(f"{quantity_id}.derived_from 必须为非空引用或引用列表")
        if any(ref not in locations and ref not in derived_ids for ref in refs):
            raise ConfigError(f"{quantity_id}.derived_from 含不可解析引用: {refs}")
        dependencies[quantity_id] = refs
        if item.get("must_not_appear_as_config"):
            if any(path.rsplit(".", 1)[-1] == quantity_id for path in business_paths):
                raise ConfigError(f"派生量不得作为配置键出现: {quantity_id}")
    for item in truths + derived_items:
        for alias in item.get("forbidden_aliases", []):
            if any(fnmatchcase(path, alias) for path in business_paths):
                raise ConfigError(f"禁止的参数别名已定义: {alias}")

    visited, active = set(), set()

    def visit(quantity_id):
        if quantity_id in active:
            raise ConfigError(f"派生量循环依赖: {quantity_id}")
        if quantity_id in visited:
            return
        active.add(quantity_id)
        for reference in dependencies[quantity_id]:
            if reference in dependencies:
                visit(reference)
        active.remove(quantity_id)
        visited.add(quantity_id)

    for quantity_id in dependencies:
        visit(quantity_id)
    reachable = set(locations)
    while True:
        new = {quantity_id for quantity_id, refs in dependencies.items()
               if all(reference in reachable for reference in refs)}
        if new <= reachable:
            break
        reachable.update(new)
    if set(dependencies) - reachable:
        raise ConfigError(f"派生量无法追溯到配置真源: {set(dependencies) - reachable}")
    logger.info("配置身份校验通过: %d 个配置路径，%d 个真源，%d 个派生量",
                len(namespaced), len(locations), len(dependencies))


def load_config_bundle(config_dir: Path | str) -> dict:
    """读取并验证全部 YAML，返回内部单位配置；地区覆盖由调用方显式应用。"""
    config_dir = Path(config_dir)
    registry = load_yaml(config_dir / "registry.yaml")
    bundle: dict[str, Any] = {"registry": registry, "regions": {}}
    declared = registry["namespaces"]
    for path in sorted(config_dir.rglob("*.yaml")):
        relative = path.relative_to(config_dir).as_posix()
        if not any(fnmatchcase(relative, role) for role in declared if role != "note"):
            raise ConfigError(f"配置文件未登记命名空间: {relative}")
        if relative == "registry.yaml":
            continue
        data = load_yaml(path)
        validate_unit_metadata(data, relative)
        if path.parent == config_dir / "regions":
            region_id = data.get("region_id")
            if not isinstance(region_id, str) or not region_id:
                raise ConfigError(f"{relative}: 缺少 region_id")
            if region_id in bundle["regions"]:
                raise ConfigError(f"地区命名空间重复: region.{region_id}")
            bundle["regions"][region_id] = data
        else:
            if path.stem in bundle:
                raise ConfigError(f"配置文件角色重复: {path.stem}")
            bundle[path.stem] = data
    validate_registry(bundle)
    return convert_units(bundle)


def storage_temperature(T_amb: np.ndarray | float, storage_site_cfg: Mapping) -> np.ndarray:
    """储能温度：户外等于环境温度；室内加偏移后按配置上下限截断。"""
    temperature = np.atleast_1d(np.asarray(T_amb, dtype=float))
    if not np.all(np.isfinite(temperature)):
        raise ValueError("环境温度必须有限")
    if storage_site_cfg["outdoor_installation"]:
        return temperature.copy()
    lower, upper = storage_site_cfg["indoor_temp_floor"], storage_site_cfg["indoor_temp_ceiling"]
    if lower > upper:
        raise ConfigError("室内温度下限高于上限")
    return np.clip(temperature + storage_site_cfg["indoor_temp_offset"], lower, upper)


@dataclass(frozen=True)
class Curve:
    """分段线性曲线，用于温度相关特性。

    Attributes:
        T: 温度断点（℃，升序），shape (n,)
        value: 对应取值，shape (n,)
    """

    T: np.ndarray
    value: np.ndarray

    def __post_init__(self) -> None:
        temperatures = np.asarray(self.T, dtype=float).copy()
        values = np.asarray(self.value, dtype=float).copy()
        if temperatures.ndim != 1 or values.ndim != 1:
            raise ValueError("Curve 的 T 与 value 必须是一维数组")
        if temperatures.shape != values.shape or not temperatures.size:
            raise ValueError("Curve 的 T 与 value 长度必须一致")
        if not np.all(np.isfinite(temperatures)) or not np.all(np.isfinite(values)):
            raise ValueError("Curve 断点和值必须有限")
        if not np.all(np.diff(temperatures) > 0):
            raise ValueError("Curve 的 T 必须严格升序")
        temperatures.setflags(write=False)
        values.setflags(write=False)
        object.__setattr__(self, "T", temperatures)
        object.__setattr__(self, "value", values)

    def __call__(self, T_celsius: np.ndarray | float) -> np.ndarray:
        """线性插值求值。低于最低断点或高于最高断点时取端点值（不外推）。"""
        T = np.atleast_1d(np.asarray(T_celsius, dtype=float))
        if not np.all(np.isfinite(T)):
            raise ValueError("曲线求值温度必须有限")
        return np.interp(T, self.T, self.value)


@dataclass
class ChemistryParams:
    """单一化学体系的参数集合。

    通过 load_chemistry() 构造，通常不需要直接实例化。
    """

    chemistry: str
    scenario: Scenario
    gamma_curve: Curve
    c_max_charge_curve: Curve
    c_max_discharge_curve: Curve
    eta_charge_curve: Curve
    eta_discharge_curve: Curve
    low_temp_charge_margin_curve: Curve
    soc_min: float
    soc_max: float
    soc_init: float
    unit_cost_energy: float       # 元/MWh（加载层已从 元/kWh 换算）
    unit_cost_power: float        # 元/MW （加载层已从 元/kW  换算）
    opex_annual: float            # 元/MWh/年（加载层已从 元/kWh/年 换算）
    cycle_life: float             # 次
    calendar_life: float          # 年
    area_per_mwh: float           # m²/MWh（加载层已从 m²/kWh 换算）
    manufacturing_kgco2_per_mwh: float   # kgCO₂/MWh（已从 kgCO₂/kWh 换算）

    # ⚠️ 上列字段全部为**内部单位**（SPEC §1.1）。加载层负责换算，
    #    业务公式内不得再出现任何 ×1000 / ÷1000 因子。
    #    字段名带内部单位后缀（_mwh / _mwh_annual），使误用一眼可辨——
    #    历史上 area_per_kwh 配 ×1000 的写法正是由于字段名仍是配置单位，
    #    调用方以为需要手工换算，造成双重换算。

    # ------------------------------------------------------------ 温度相关特性

    def gamma(self, T_celsius: np.ndarray | float) -> np.ndarray:
        """低温可用容量系数；高情景缩放允许 >1，仅在 usable_band 截断。

        这是本方案的关键机制：名义容量在温度 T 下可实际调用的比例。
        直接进入 SOC 上界（或等效地限制可用能量区间）。
        """
        return self.gamma_curve(T_celsius)

    def c_max_charge(self, T_celsius: np.ndarray | float) -> np.ndarray:
        """最大充电倍率（1/h），已包含低温安全裕度。

        注意：返回值已乘上 low_temp_charge_margin，调用方无需再乘一次。
        """
        base = self.c_max_charge_curve(T_celsius)
        margin = self.low_temp_charge_margin_curve(T_celsius)
        return base * margin

    def c_max_discharge(self, T_celsius: np.ndarray | float) -> np.ndarray:
        """最大放电倍率（1/h）。"""
        return self.c_max_discharge_curve(T_celsius)

    def eta_charge(self, T_celsius: np.ndarray | float) -> np.ndarray:
        """充电效率。"""
        return self.eta_charge_curve(T_celsius)

    def eta_discharge(self, T_celsius: np.ndarray | float) -> np.ndarray:
        """放电效率。"""
        return self.eta_discharge_curve(T_celsius)

    def round_trip_efficiency(self, T_celsius: np.ndarray | float) -> np.ndarray:
        """往返效率 = eta_charge * eta_discharge。"""
        return self.eta_charge(T_celsius) * self.eta_discharge(T_celsius)

    # ------------------------------------------------------------ 派生量

    def usable_band(
        self, T_storage: np.ndarray, soc_ref: float | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """温度相关的可用 SOC 带（SPEC §2.3）。

            half_band = 0.5 · (soc_max − soc_min)
            soc_hi(T) = soc_ref + γ(T) · half_band
            soc_lo(T) = soc_ref − γ(T) · half_band

        **以 soc_ref 为中心对称收缩**，保证初始状态恒可行（v1.1 的收缩上界写法
        会在 -25℃ 下给出低于 soc_init 的上界，导致不可行）。

        γ 的作用**只在这里**，不进入 SOC 动态。这样：
          - 累计可用能量按 γ 折减（γ = 0.5 时可放出量为标称的一半）
          - 充放往返无 γ² 损耗（充 1 MWh 可放出 1 MWh，η = 1 时）
          - 温度骤降不会导致不可行（约束加在能量流上，见 storage.energy_limits）

        Args:
            T_storage: 逐时段储能温度（℃）
            soc_ref: 工作点，默认取 soc_init

        Returns:
            (soc_lo, soc_hi)，与 T_storage 同形状
        """
        reference = self.soc_init if soc_ref is None else float(soc_ref)
        if not np.isfinite(reference) or not self.soc_min <= reference <= self.soc_max:
            raise ValueError("soc_ref 必须位于标称 SOC 窗口内")
        half_band = 0.5 * (self.soc_max - self.soc_min)
        width = self.gamma(T_storage) * half_band
        return np.maximum(self.soc_min, reference - width), np.minimum(self.soc_max, reference + width)

    def soc_bounds(self) -> tuple[float, float]:
        """返回标称 SOC 窗口（soc_min, soc_max）。

        ⚠️ **恒定，与温度无关**（SPEC §2.3）。

        低温对可用能量的影响通过 `usable_band(T)` 表达，不通过收缩本窗口表达。
        若实现为 soc_max × γ(T)，锂电在 -25℃ 时上界约 0.48 < soc_init = 0.50，
        MILP 开局即不可行——而这是北方冬季的典型工况。
        """
        return self.soc_min, self.soc_max

    def power_bounds(
        self, E_nom: float, P_nom: float, T_celsius: np.ndarray | float
    ) -> tuple[np.ndarray, np.ndarray]:
        """返回 (p_ch_max, p_dis_max)，单位 MW。

        功率上限取"额定功率"与"倍率约束"的较小者：

            p_ch_max  = min( P_nom , c_max_charge(T)  × E_nom )
            p_dis_max = min( P_nom , c_max_discharge(T) × E_nom )

        ⚠️ **不再乘 γ**（SPEC §2.3）。
        γ 只作用于可用能量边界（见 usable_band），若在此处也乘会造成双重折减。

        `c_max_charge` 已内含 `low_temp_charge_margin`（低温充电安全裕度），
        调用方不要再乘一次。
        """
        if not np.isfinite(E_nom) or not np.isfinite(P_nom) or E_nom < 0 or P_nom < 0:
            raise ValueError("额定容量与功率必须是有限非负值")
        return (
            np.minimum(P_nom, self.c_max_charge(T_celsius) * E_nom),
            np.minimum(P_nom, self.c_max_discharge(T_celsius) * E_nom),
        )


def _scalar(entry: Any, scenario: Scenario, name: str) -> float:
    if isinstance(entry, Mapping):
        if all(key in entry for key in _SCENARIOS):
            values = [float(entry[key]) for key in _SCENARIOS]
            if not np.all(np.isfinite(values)) or not values[0] <= values[1] <= values[2]:
                raise ConfigError(f"{name}: 区间必须 low ≤ mid ≤ high")
            value = float(entry[scenario])
        elif "value" in entry:
            value = float(entry["value"])
        else:
            raise ConfigError(f"{name}: 参数块缺少 value 或 low/mid/high")
    else:
        value = float(entry)
    if not np.isfinite(value):
        raise ConfigError(f"{name}: 必须是有限数值")
    return value


def _load_curve(block: Mapping, scenario: Scenario, name: str, bounded: bool) -> Curve:
    scales = np.asarray(block["scale_range"], dtype=float)
    if scales.shape != (3,) or not np.all(np.isfinite(scales)) or np.any(scales <= 0):
        raise ConfigError(f"{name}: scale_range 必须为三个正数")
    if scales[1] != 1.0 or not np.all(np.diff(scales) >= 0):
        raise ConfigError(f"{name}: scale_range 必须升序且 mid=1")
    scale = scales[_SCENARIOS.index(scenario)]
    points = block["points"]
    values = np.array([point["value"] for point in points], dtype=float) * scale
    if not np.all(np.isfinite(values)) or np.any(values <= 0):
        raise ConfigError(f"{name}: 曲线值必须有限且 > 0")
    if bounded:
        values = np.minimum(values, 1.0)
    return Curve(np.array([point["T"] for point in points], dtype=float), values)


def load_chemistry(
    chem: str,
    config_dir: Path,
    scenario: Scenario = "mid",
) -> ChemistryParams:
    """从 configs/chemistry_{chem}.yaml 加载参数。

    Args:
        chem: "na" 或 "li"
        config_dir: configs 目录
        scenario: "low" / "mid" / "high"，用于敏感性扫描

    Returns:
        ChemistryParams

    Notes:
        读取时若某项仍是 PLACEHOLDER（source 字段以 "PLACEHOLDER" 开头），
        应在首次调用时通过 logging.warning 提示一次，便于确认标定进度。
    """
    if chem not in ("na", "li"):
        raise ValueError("chem 必须是 'na' 或 'li'")
    if scenario not in _SCENARIOS:
        raise ValueError("scenario 必须是 low / mid / high")
    path = Path(config_dir) / f"chemistry_{chem}.yaml"
    raw = load_yaml(path)
    if raw.get("chemistry") != chem:
        raise ConfigError(f"{path}: chemistry 与文件角色不一致")
    resolved_path = path.resolve()
    placeholders = sum(
        isinstance(value, str) and value.upper().startswith("PLACEHOLDER")
        for key, value in _walk_nodes(raw) if key.rsplit(".", 1)[-1] == "source"
    )
    if placeholders and resolved_path not in _PLACEHOLDER_WARNED:
        logger.warning("%s: %d 项 PLACEHOLDER 参数，当前结果仅为仿真", path.name, placeholders)
        _PLACEHOLDER_WARNED.add(resolved_path)
    return chemistry_params_from_config(raw, scenario)


def chemistry_params_from_config(
    config: Mapping, scenario: Scenario = "mid"
) -> ChemistryParams:
    """由原始或已换算的配置块装配参数；与文件加载共用唯一装配规则，输入不变。"""
    if scenario not in _SCENARIOS:
        raise ValueError("scenario 必须是 low / mid / high")
    chem = config.get("chemistry")
    if chem not in ("na", "li"):
        raise ConfigError("chemistry 必须是 'na' 或 'li'")
    validate_unit_metadata(config, f"chemistry_{chem}")
    cfg = convert_units(config)
    curves = {}
    for section, name, bounded in (
        ("temp_derating", "gamma", False),
        ("temp_derating", "c_max_charge", False),
        ("temp_derating", "c_max_discharge", False),
        ("efficiency", "eta_charge", True),
        ("efficiency", "eta_discharge", True),
        ("safety", "low_temp_charge_margin", True),
    ):
        curves[f"{name}_curve"] = _load_curve(cfg[section][name], scenario, name, bounded)
    values = {}
    for section, key, field in (
        ("cost", "unit_cost_energy", "unit_cost_energy"),
        ("cost", "unit_cost_power", "unit_cost_power"),
        ("cost", "opex_annual", "opex_annual"),
        ("lifetime", "cycle_life", "cycle_life"),
        ("lifetime", "calendar_life", "calendar_life"),
        ("footprint", "area_per_kwh", "area_per_mwh"),
        ("lcc", "manufacturing_kgco2_per_kwh", "manufacturing_kgco2_per_mwh"),
    ):
        values[field] = _scalar(cfg[section][key], scenario, f"{section}.{key}")
        if values[field] < 0 or (section == "lifetime" and values[field] == 0):
            raise ConfigError(f"{section}.{key}: 必须非负，寿命须 > 0")
    soc_min, soc_max, soc_init = (float(cfg["soc"][key]) for key in ("min", "max", "init"))
    if not 0 <= soc_min <= soc_init <= soc_max <= 1 or soc_min == soc_max:
        raise ConfigError("SOC 必须满足 0 ≤ min ≤ init ≤ max ≤ 1 且 min < max")
    return ChemistryParams(
        chemistry=chem, scenario=scenario, soc_min=soc_min, soc_max=soc_max,
        soc_init=soc_init, **curves, **values,
    )


def apply_lithium_price_scenario(
    params: ChemistryParams, multiplier: float
) -> ChemistryParams:
    """锂价情景：按倍率调整锂电单位能量造价（见 SPEC §6.5 敏感性第二组）。

    仅调整 unit_cost_energy，不改变任何物理参数。
    """
    if params.chemistry != "li":
        raise ValueError("锂价情景只能用于锂电参数")
    if not np.isfinite(multiplier) or multiplier <= 0:
        raise ValueError("锂价倍率必须是有限正数")
    return replace(params, unit_cost_energy=params.unit_cost_energy * multiplier)
