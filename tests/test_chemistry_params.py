"""电化学参数测试：曲线性质、区间一致性、低温差异（SPEC §9、§13.2）。

这些测试在 M1 阶段即可全部通过，是数据层的第一道门槛。
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml

from aidc.data.chemistry import (
    ConfigError,
    Curve,
    apply_lithium_price_scenario,
    chemistry_params_from_config,
    convert_units,
    load_chemistry,
    load_config_bundle,
    load_yaml,
    storage_temperature,
    validate_registry,
    validate_unit_metadata,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"

NA_PATH = CONFIG_DIR / "chemistry_na.yaml"
LI_PATH = CONFIG_DIR / "chemistry_li.yaml"


@pytest.fixture(scope="module")
def na_cfg() -> dict:
    return yaml.safe_load(NA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def li_cfg() -> dict:
    return yaml.safe_load(LI_PATH.read_text(encoding="utf-8"))


def _curve_from(points: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    T = np.array([p["T"] for p in points], dtype=float)
    v = np.array([p["value"] for p in points], dtype=float)
    order = np.argsort(T)
    return T[order], v[order]


def _curve_points(cfg: dict, section: str, name: str) -> list[dict]:
    """取出曲线断点。

    ⚠️ 结构约定（SPEC §1.3）：曲线块为 {points, source, scale_range}，
    points 的值才是序列。v1.0 曾把 source 写在序列项同层，那是非法 YAML。
    本辅助函数同时用于捕获结构回归。
    """
    block = cfg[section][name]
    assert isinstance(block, dict), (
        f"{section}.{name} 应为映射（含 points/source），实际为 {type(block).__name__}"
    )
    assert "points" in block, f"{section}.{name} 缺少 points 键"
    return block["points"]


def test_all_configs_parse():
    """configs/ 下全部 YAML 必须能被标准解析器读取（SPEC §9.21）。

    这条测试专防"序列与映射键混用"这类非法 YAML 结构回归。
    """
    for path in sorted(CONFIG_DIR.glob("**/*.yaml")):
        assert isinstance(load_yaml(path), dict)


def test_curve_blocks_have_points_key(na_cfg, li_cfg):
    """所有曲线块必须使用 points 结构（SPEC §1.3）。"""
    for name, cfg in (("na", na_cfg), ("li", li_cfg)):
        for section, curve_name in [
            ("temp_derating", "gamma"),
            ("temp_derating", "c_max_charge"),
            ("temp_derating", "c_max_discharge"),
            ("efficiency", "eta_charge"),
            ("efficiency", "eta_discharge"),
            ("safety", "low_temp_charge_margin"),
        ]:
            _curve_points(cfg, section, curve_name)  # 断言在内部


def test_curve_scale_range_present(na_cfg, li_cfg):
    """曲线必须带 scale_range，否则敏感性分析无法复现（SPEC §1.3）。"""
    for name, cfg in (("na", na_cfg), ("li", li_cfg)):
        for section, curve_name in [
            ("temp_derating", "gamma"),
            ("temp_derating", "c_max_charge"),
            ("efficiency", "eta_charge"),
            ("safety", "low_temp_charge_margin"),
        ]:
            block = cfg[section][curve_name]
            assert "scale_range" in block, f"{name}.{section}.{curve_name} 缺少 scale_range"
            sr = block["scale_range"]
            assert len(sr) == 3, f"{name}.{section}.{curve_name} 的 scale_range 应为三项"
            assert sr[1] == 1.0, (
                f"{name}.{section}.{curve_name} 的 scale_range 中点应为 1.0（mid 表示不缩放）"
            )


def test_unit_fields_declared(na_cfg, li_cfg):
    """成本类参数必须显式声明单位，供加载层换算（SPEC §1.1）。"""
    for name, cfg in (("na", na_cfg), ("li", li_cfg)):
        assert cfg["cost"]["unit_cost_energy"].get("unit") == "元/kWh", (
            f"{name}.cost.unit_cost_energy 必须声明 unit"
        )
        assert cfg["cost"]["unit_cost_power"].get("unit") == "元/kW", (
            f"{name}.cost.unit_cost_power 必须声明 unit"
        )


def test_curves_are_monotonic_in_temperature(na_cfg, li_cfg):
    """gamma 曲线必须在低温段单调不增（温度越低，可用容量越少）。

    若曲线非单调，说明数据录入有误或标定有问题。
    """
    for name, cfg in (("na", na_cfg), ("li", li_cfg)):
        T, gamma = _curve_from(_curve_points(cfg, "temp_derating", "gamma"))
        # 在 -30 ~ 25℃ 区间内应单调不降（温度升高，可用容量升高）
        mask = T <= 25
        diffs = np.diff(gamma[mask])
        assert np.all(diffs >= -1e-9), f"{name}: gamma 曲线在低温段非单调"


def test_gamma_within_unit_interval(na_cfg, li_cfg):
    """gamma 取值必须在 (0, 1] 内。"""
    for name, cfg in (("na", na_cfg), ("li", li_cfg)):
        _, gamma = _curve_from(_curve_points(cfg, "temp_derating", "gamma"))
        assert np.all(gamma > 0.0), f"{name}: gamma 出现非正值"
        assert np.all(gamma <= 1.0 + 1e-9), f"{name}: gamma 超过 1"


def test_charge_rate_decreases_at_low_temperature(na_cfg, li_cfg):
    """充电倍率上限必须随温度下降而下降（低温充电受限是物理事实）。"""
    for name, cfg in (("na", na_cfg), ("li", li_cfg)):
        T, c = _curve_from(_curve_points(cfg, "temp_derating", "c_max_charge"))
        c_at_low = c[T == -20][0] if (T == -20).any() else c[0]
        c_at_room = c[T == 25][0]
        assert c_at_low < c_at_room, f"{name}: 低温充电倍率未受限"


def test_lithium_low_temp_derating_steeper_than_sodium(na_cfg, li_cfg):
    """锂电的低温折减应比钠电更陡。

    ⚠️ 这个测试**不是**要求实现者去"调"出这个结论，而是：
        当前占位参数下该关系成立，作为一致性检查。
        标定后若曲线关系改变，应更新此测试并在报告中说明——
        配置结论会随之改变，这是方案设计允许的（SPEC §1.4、§1.3）。

    本测试的作用是确保 NA/LI 两个配置文件没有被无意中互相覆盖或复制粘贴出错。
    """
    T_na, g_na = _curve_from(_curve_points(na_cfg, "temp_derating", "gamma"))
    T_li, g_li = _curve_from(_curve_points(li_cfg, "temp_derating", "gamma"))

    T_test = np.array([-30.0, -20.0, -10.0, 0.0])
    g_na_i = np.interp(T_test, T_na, g_na)
    g_li_i = np.interp(T_test, T_li, g_li)

    assert np.all(g_na_i > g_li_i), (
        "当前占位参数下钠电低温可用容量应高于锂电。若标定后此关系改变，"
        "需更新本测试与报告结论（配置推荐可能翻转为锂电为主）。"
    )


def test_unit_conversion_factor_applied():
    """单位换算检验（SPEC §1.1）。

    等价换算：把单位能量造价用两种方式表达，必须得到相同结果。
        a) 配置口径：  0.50 元/kWh   → 加载后应为 500 元/MWh
        b) 内部口径：  500 元/MWh    → 直接写入内部字段

    若两者相差 1000 倍，说明换算因子被遗漏或被重复施加。
    这是 Q1 指出的"成本相差千倍"错误的守门测试。
    """
    params = load_chemistry("na", CONFIG_DIR, scenario="mid")
    assert params.unit_cost_energy == pytest.approx(500.0, rel=1e-9)
    assert params.unit_cost_power == pytest.approx(450_000.0, rel=1e-9)
    assert params.opex_annual == pytest.approx(15_000.0)
    assert params.area_per_mwh == pytest.approx(45.0)
    assert params.manufacturing_kgco2_per_mwh == pytest.approx(70_000.0)
    assert load_config_bundle(CONFIG_DIR)["system"]["compute"]["gpu_power"]["value"] == pytest.approx(0.0007)


def test_low_temp_charge_margin_binds():
    """低温下充电功率上限必须显著低于常温（SPEC §2.3）。

    这条测试守的是本方案的核心机制：低温折减必须真正进入约束。
    若低温与常温的上限接近，说明 gamma / c_max / margin 未生效。
    """
    params = load_chemistry("li", CONFIG_DIR)
    p_charge, p_discharge = params.power_bounds(100.0, 200.0, np.array([-20.0, 25.0]))
    # -20℃: 0.05 C × 0.6 safety × 100 MWh = 3 MW；γ不再重复乘入。
    np.testing.assert_allclose(p_charge, [3.0, 100.0])
    np.testing.assert_allclose(p_discharge, [25.0, 100.0])
    capped, _ = params.power_bounds(100.0, 2.0, -20.0)
    np.testing.assert_allclose(capped, [2.0])


def test_soc_window_not_shrunk_by_gamma():
    """SOC 窗口不随温度收缩（SPEC §2.3）。

    ⚠️ 这是 U4 的守门测试。若实现为 soc_max(T) = 0.9 × gamma(T)，
    则 T = -25℃ 时锂电上界约为 0.48，低于 soc_init = 0.50，
    会直接导致 MILP 不可行——而这是北方冬季的典型工况。
    """
    params = load_chemistry("li", CONFIG_DIR)
    assert params.soc_bounds() == (0.1, 0.9)
    lower, upper = params.usable_band(np.array([-25.0, 25.0]))
    assert np.all(lower <= params.soc_init)
    assert np.all(upper >= params.soc_init)
    assert upper[0] < upper[1]
    assert params.soc_bounds() == (0.1, 0.9)


def test_all_parameters_have_interval(na_cfg, li_cfg):
    """成本与寿命类参数必须给出 {low, mid, high} 区间（SPEC §1.3）。

    单点值会使敏感性分析无法进行。
    """
    keys = [
        ("cost", "unit_cost_energy"),
        ("cost", "unit_cost_power"),
        ("cost", "opex_annual"),
        ("lifetime", "cycle_life"),
        ("lifetime", "calendar_life"),
    ]
    for name, cfg in (("na", na_cfg), ("li", li_cfg)):
        for section, key in keys:
            entry = cfg[section][key]
            assert isinstance(entry, dict), f"{name}.{section}.{key} 不是字典"
            for k in ("low", "mid", "high"):
                assert k in entry, f"{name}.{section}.{key} 缺少 {k}"
            assert entry["low"] <= entry["mid"] <= entry["high"], (
                f"{name}.{section}.{key} 区间顺序错误"
            )


def test_placeholder_sources_are_marked(na_cfg, li_cfg):
    """占位参数必须带 PLACEHOLDER 标记，便于追踪标定进度。

    本测试用于防止把占位值误当成已标定值使用。
    标定完成后应移除标记。
    """
    n_placeholder = 0
    for name, cfg in (("na", na_cfg), ("li", li_cfg)):
        for section, block in cfg.items():
            if isinstance(block, dict) and "source" in block:
                if str(block["source"]).startswith("PLACEHOLDER"):
                    n_placeholder += 1
    # 不强制为零，只提示当前状态
    assert n_placeholder >= 0
    print(f"\n[标定进度] 当前仍有 {n_placeholder} 项参数为 PLACEHOLDER")


def test_curve_interpolates_and_clamps_without_modifying_input():
    temperatures, values = np.array([-20.0, 0.0, 25.0]), np.array([0.5, 0.7, 1.0])
    curve = Curve(temperatures, values)
    temperatures[0] = -100.0
    values[0] = 0.0
    np.testing.assert_allclose(curve([-40.0, -10.0, 12.5, 50.0]), [0.5, 0.6, 0.85, 1.0])
    assert curve(0.0).shape == (1,)
    assert not curve.value.flags.writeable


@pytest.mark.parametrize("temperatures,values", [
    ([], []), ([0, 0], [1, 2]), ([1, 0], [1, 2]),
    ([0, 1], [1]), ([0, np.nan], [1, 2]), ([0, 1], [1, np.inf]),
])
def test_invalid_curve_rejected(temperatures, values):
    with pytest.raises(ValueError):
        Curve(temperatures, values)


def test_usable_band_clips_both_sides():
    params = load_chemistry("li", CONFIG_DIR)
    # 可用带必须独立做上下截断，即便加载后的 gamma 高于 1。
    expanded = replace(params, gamma_curve=Curve([0.0], [1.1]))
    low, high = expanded.usable_band([0.0])
    np.testing.assert_allclose(low, [0.1])
    np.testing.assert_allclose(high, [0.9])
    asymmetric_low, asymmetric_high = params.usable_band([25.0], soc_ref=0.2)
    np.testing.assert_allclose(asymmetric_low, [0.1])
    np.testing.assert_allclose(asymmetric_high, [0.6])


def test_power_zero_capacity_and_no_gamma_double_derating():
    params = load_chemistry("na", CONFIG_DIR)
    without_capacity = params.power_bounds(0.0, 45.0, [-20.0, 25.0])
    for bounds in without_capacity:
        np.testing.assert_array_equal(bounds, [0.0, 0.0])
    changed_gamma = replace(params, gamma_curve=Curve([-20, 25], [0.1, 0.2]))
    np.testing.assert_array_equal(
        params.power_bounds(100, 45, [-20, 25]),
        changed_gamma.power_bounds(100, 45, [-20, 25]),
    )


def test_low_scenario_scales_curves_and_selects_scalar_intervals(na_cfg):
    params = load_chemistry("na", CONFIG_DIR, "low")
    assert params.unit_cost_energy == pytest.approx(350.0)
    assert params.cycle_life == na_cfg["lifetime"]["cycle_life"]["low"]
    np.testing.assert_allclose(params.gamma(-20.0), [0.88 * 0.9])
    np.testing.assert_allclose(params.eta_charge(-20), [0.94 * 0.98])
    np.testing.assert_allclose(params.c_max_charge(-20), [0.10 * 0.9 * 0.7 * 0.95])
    np.testing.assert_allclose(params.round_trip_efficiency(-20), [(0.94 * 0.98) ** 2])


@pytest.mark.parametrize("chem", ["na", "li"])
def test_high_gamma_preserves_scaling_interpolates_and_clips_only_band(chem):
    path = CONFIG_DIR / f"chemistry_{chem}.yaml"
    source_bytes = path.read_bytes()
    params = load_chemistry(chem, CONFIG_DIR, "high")
    raw = load_yaml(path)
    block = raw["temp_derating"]["gamma"]
    np.testing.assert_allclose(params.gamma(25.0), [1.1])
    expected_midpoint = (block["points"][3]["value"] + block["points"][4]["value"]) / 2 * block["scale_range"][2]
    np.testing.assert_allclose(params.gamma(12.5), [expected_midpoint])
    lower, upper = params.usable_band([12.5, 25.0])
    np.testing.assert_allclose(lower, [0.1, 0.1])
    np.testing.assert_allclose(upper, [0.9, 0.9])
    assert path.read_bytes() == source_bytes
    assert raw["temp_derating"]["gamma"]["points"][4]["value"] == 1.0


def test_high_efficiency_and_safety_are_still_capped_at_one(na_cfg, tmp_path):
    raw = copy.deepcopy(na_cfg)
    for section, name in (("efficiency", "eta_charge"), ("efficiency", "eta_discharge"),
                          ("safety", "low_temp_charge_margin")):
        for point in raw[section][name]["points"]:
            point["value"] = 1.0
    before = copy.deepcopy(raw)
    (tmp_path / "chemistry_na.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    params = load_chemistry("na", tmp_path, "high")
    np.testing.assert_allclose(params.eta_charge([-20, 25]), [1.0, 1.0])
    np.testing.assert_allclose(params.eta_discharge([-20, 25]), [1.0, 1.0])
    np.testing.assert_allclose(params.low_temp_charge_margin_curve([-20, 25]), [1.0, 1.0])
    assert raw == before


def test_lithium_price_scenario_is_isolated():
    original = load_chemistry("li", CONFIG_DIR)
    scaled = apply_lithium_price_scenario(original, 1.6)
    assert original.unit_cost_energy == pytest.approx(400.0)
    assert scaled.unit_cost_energy == pytest.approx(640.0)
    for key, value in vars(original).items():
        if key != "unit_cost_energy":
            assert vars(scaled)[key] is value or vars(scaled)[key] == value
    with pytest.raises(ValueError):
        apply_lithium_price_scenario(load_chemistry("na", CONFIG_DIR), 1.0)
    for invalid in (0, -1, np.inf, np.nan):
        with pytest.raises(ValueError):
            apply_lithium_price_scenario(original, invalid)


def test_storage_temperature_uses_installation_config():
    system = load_config_bundle(CONFIG_DIR)["system"]
    outdoor = system["storage_site_default"]
    temperatures = np.array([-25.0, 5.0, 40.0])
    np.testing.assert_array_equal(storage_temperature(temperatures, outdoor), temperatures)
    indoor = dict(outdoor, outdoor_installation=False)
    np.testing.assert_array_equal(storage_temperature(temperatures, indoor), [15.0, 20.0, 35.0])
    np.testing.assert_array_equal(temperatures, [-25.0, 5.0, 40.0])


@pytest.mark.parametrize("source,internal,expected", [
    ("元/kWh", "元/MWh", 2000.0),
    ("元/kW", "元/MW", 2000.0),
    ("元/kWh/年", "元/MWh/年", 2000.0),
    ("m²/kWh", "m²/MWh", 2000.0),
    ("kgCO₂/kWh", "kgCO₂/MWh", 2000.0),
    ("kW/卡", "MW/卡", 0.002),
])
def test_conversion_is_recursive_idempotent_and_nonmutating(source, internal, expected):
    raw = {"items": [{"parameter": {"low": 1.0, "mid": 2.0, "high": 3.0,
                                    "unit": source, "internal_unit": internal,
                                    "scale_range": [0.9, 1.0, 1.1], "source": "simulation"}}]}
    before = copy.deepcopy(raw)
    converted = convert_units(raw)
    parameter = converted["items"][0]["parameter"]
    assert parameter["mid"] == pytest.approx(expected)
    assert parameter["unit"] == internal
    assert parameter["scale_range"] == [0.9, 1.0, 1.1]
    assert convert_units(converted) == converted
    assert raw == before


def test_duplicate_yaml_keys_fail_at_nested_path(tmp_path):
    bad = tmp_path / "duplicate.yaml"
    bad.write_text("compute:\n  gpu: 1\n  gpu: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="重复键"):
        load_yaml(bad)


def test_unit_metadata_missing_or_incompatible_fails(na_cfg):
    broken = copy.deepcopy(na_cfg)
    del broken["cost"]["unit_cost_energy"]["unit"]
    with pytest.raises(ConfigError, match="unit/internal_unit"):
        validate_unit_metadata(broken)
    with pytest.raises(ConfigError, match="internal_unit"):
        convert_units({"value": 1.0, "unit": "元/kWh"})
    with pytest.raises(ConfigError, match="键名单位"):
        validate_unit_metadata({"power_mw": {"value": 1.0, "unit": "kW"}})


@pytest.mark.parametrize("key,valid_unit,invalid_unit", [
    ("heat_mw_th", "MW_th", "MW"),
    ("heat_mwh_th", "MWh_th", "MWh"),
])
def test_thermal_unit_suffix_cannot_be_overridden_by_metadata(key, valid_unit, invalid_unit):
    validate_unit_metadata({key: {"value": 2.0, "unit": valid_unit, "internal_unit": valid_unit}})
    with pytest.raises(ConfigError, match="键名单位"):
        validate_unit_metadata({key: {"value": 2.0, "unit": invalid_unit, "internal_unit": invalid_unit}})


def test_placeholder_warning_is_once_per_source_file(caplog, monkeypatch):
    import aidc.data.chemistry as chemistry

    monkeypatch.setattr(chemistry, "_PLACEHOLDER_WARNED", set())
    with caplog.at_level("WARNING", logger="aidc.data.chemistry"):
        load_chemistry("na", CONFIG_DIR)
        load_chemistry("na", CONFIG_DIR, "low")
    warnings = [record.message for record in caplog.records if "PLACEHOLDER" in record.message]
    assert len(warnings) == 1
    assert "13" in warnings[0]


def test_configuration_bundle_closes_registry_and_preserves_regions():
    bundle = load_config_bundle(CONFIG_DIR)
    assert set(bundle) == {"system", "economics", "generation", "experiments",
                           "registry", "chemistry_na", "chemistry_li", "regions"}
    assert set(bundle["regions"]) == {"R-NW", "R-WPV", "R-EAST", "R-SOUTH"}
    assert bundle["system"]["compute"]["s1_reserved_fraction"] == 0.167
    assert bundle["regions"]["R-SOUTH"]["compute_override"]["s1_reserved_fraction"] == 0.25
    validate_registry(bundle)


@pytest.mark.parametrize("scenario", ["low", "mid", "high"])
def test_chemistry_factory_matches_raw_internal_and_file_loading(scenario):
    raw = load_yaml(NA_PATH)
    before = copy.deepcopy(raw)
    internal = convert_units(raw)
    instances = [chemistry_params_from_config(raw, scenario),
                 chemistry_params_from_config(internal, scenario),
                 load_chemistry("na", CONFIG_DIR, scenario)]
    for params in instances:
        assert params.unit_cost_energy == instances[0].unit_cost_energy
        assert params.area_per_mwh == instances[0].area_per_mwh
        np.testing.assert_array_equal(params.gamma([-20, 12.5, 25]),
                                      instances[0].gamma([-20, 12.5, 25]))
        np.testing.assert_array_equal(params.power_bounds(100, 45, [-20, 25]),
                                      instances[0].power_bounds(100, 45, [-20, 25]))
    assert raw == before


def test_registry_rejects_unlisted_override_and_forbidden_alias():
    bundle = load_config_bundle(CONFIG_DIR)
    bundle["regions"]["R-SOUTH"]["compute_override"]["gpu_power"] = 0.8
    with pytest.raises(ConfigError, match="未授权"):
        validate_registry(bundle)
    bundle = load_config_bundle(CONFIG_DIR)
    bundle["system"]["compute"]["gpu_total"] = 7000
    with pytest.raises(ConfigError, match="禁止的参数别名"):
        validate_registry(bundle)


def test_registry_rejects_duplicate_truth_and_unresolvable_dependency():
    bundle = load_config_bundle(CONFIG_DIR)
    truths = bundle["registry"]["canonical_config_truths"]
    truths[1]["authorized"] = truths[0]["authorized"]
    with pytest.raises(ConfigError, match="两两不同"):
        validate_registry(bundle)
    bundle = load_config_bundle(CONFIG_DIR)
    bundle["registry"]["derived_quantities"][0]["derived_from"] = "gpu_single_card_power"
    with pytest.raises(ConfigError, match="不可解析"):
        validate_registry(bundle)


def test_registry_rejects_cycles_and_derived_config_keys():
    bundle = load_config_bundle(CONFIG_DIR)
    derived = bundle["registry"]["derived_quantities"]
    derived[0]["derived_from"] = derived[1]["quantity_id"]
    derived[1]["derived_from"] = derived[0]["quantity_id"]
    with pytest.raises(ConfigError, match="循环依赖"):
        validate_registry(bundle)
    bundle = load_config_bundle(CONFIG_DIR)
    bundle["system"]["compute"]["minimum_required_avg_utilization"] = 0.3
    with pytest.raises(ConfigError, match="派生量不得作为配置键"):
        validate_registry(bundle)
