"""Deterministic simulated inputs: 8928 hours, first 8760 for annual statistics.

CF references are diagnostics only; generated values never depend on them.
Representative weeks are independent windows, with no implied SOC connection.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Literal
import warnings

import numpy as np
import pandas as pd

Fidelity = Literal["low", "high"]
HOURLY_COLUMNS = [
    "T_amb", "P_pv_avail", "P_wind_avail", "price_buy", "price_sell",
    "CI_grid", "IT_base", "Q_mun_demand",
]
SIMULATION_NOTICE = "基于仿真生成的气象与负载序列，非真实设备或站点数据。"


def _number(value, name: str) -> float:
    if isinstance(value, dict):
        value = value["value"] if "value" in value else value["mid"]
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric, not boolean")
    result = float(value)
    if not np.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _positive_integer(value, name: str) -> int:
    result = _number(value, name)
    if result <= 0 or result != int(result):
        raise ValueError(f"{name} must be a positive integer")
    return int(result)


def _validate_hourly(ts: pd.DataFrame) -> None:
    if not isinstance(ts.index, pd.DatetimeIndex):
        raise ValueError("time series must have a DatetimeIndex")
    if ts.empty or ts.index.has_duplicates or not ts.index.is_monotonic_increasing:
        raise ValueError("time series index must be nonempty, unique and increasing")
    if len(ts) > 1 and not (np.diff(ts.index.asi8) == pd.Timedelta(hours=1).value).all():
        raise ValueError("time series must be continuous with one-hour intervals")


def _weather_indices(index: pd.DatetimeIndex, cfg: dict, rng) -> tuple[np.ndarray, np.ndarray]:
    """Daily Markov weather, initialized at its derived stationary distribution."""
    states = list(cfg["weather_markov"]["states"])
    transition = np.asarray([cfg["weather_markov"]["transition"][s] for s in states], float)
    if not states or transition.shape != (len(states), len(states)):
        raise ValueError("weather transition matrix must match its state list")
    if not np.isfinite(transition).all() or (transition < 0).any():
        raise ValueError("weather probabilities must be finite and nonnegative")
    if not np.allclose(transition.sum(axis=1), 1, rtol=0, atol=1e-12):
        raise ValueError("weather transition rows must sum to one")
    matrix = np.vstack((transition.T - np.eye(len(states)), np.ones(len(states))))
    stationary = np.linalg.lstsq(matrix, np.r_[np.zeros(len(states)), 1.0], rcond=None)[0]
    stationary = np.maximum(stationary, 0)
    stationary /= stationary.sum()
    day_codes, days = pd.factorize(index.normalize(), sort=False)
    daily = np.empty(len(days), dtype=int)
    daily[0] = rng.choice(len(states), p=stationary)
    for day in range(1, len(days)):
        daily[day] = rng.choice(len(states), p=transition[daily[day - 1]])
    state = daily[day_codes]
    means = np.asarray([_number(cfg["clearness_index"][s]["mean"], s) for s in states])
    stds = np.asarray([_number(cfg["clearness_index"][s]["std"], s) for s in states])
    if (stds < 0).any() or ((means < 0) | (means > 1)).any():
        raise ValueError("clearness means must be in [0, 1] and stds nonnegative")
    return np.asarray(states, object)[state], np.clip(rng.normal(means[state], stds[state]), 0, 1)


def build_year_timeseries(
    region_cfg: dict, system_cfg: dict, econ_cfg: dict, gen_cfg: dict,
    seed: int, year: int = 2026,
) -> pd.DataFrame:
    """Generate simulated hourly data using raw or already converted configs.

    Unit conversion is delegated to the idempotent loading-layer helper.
    Shape choices not fixed by SPEC are recorded in attrs: calendar-boundary
    winter cosine; asymmetric daily temperature cosine; local solar time and
    horizontal-plane solar elevation; daily Markov weather; AR-smoothed Weibull
    wind; climate-normalized seasonal heat. Carbon uses its configured mean:
    no fluctuation amplitude exists in config, so none is silently invented.
    """
    from aidc.data.chemistry import convert_units

    region, system, economics, generation = [
        convert_units(deepcopy(cfg)) for cfg in (region_cfg, system_cfg, econ_cfg, gen_cfg)
    ]
    series = generation["series"]
    year_hours = _positive_integer(series["year_hours"], "series.year_hours")
    extra_hours = _positive_integer(series["extra_hours"], "series.extra_hours")
    total_hours = _positive_integer(series["total_hours"], "series.total_hours")
    if total_hours != year_hours + extra_hours:
        raise ValueError("total_hours must equal year_hours + extra_hours")
    index = pd.date_range(f"{int(year)}-01-01", periods=total_hours, freq="h")
    rng = np.random.default_rng(seed)
    hour = index.hour.to_numpy(dtype=float)
    days_in_year = np.where(index.is_leap_year, 366.0, 365.0)
    day = index.dayofyear.to_numpy(dtype=float)
    winter = np.cos(2 * np.pi * (day - 1 + hour / 24) / days_in_year)
    climate = region["climate"]
    t_min = _number(climate["T_min_celsius"], "T_min_celsius")
    t_max = _number(climate["T_max_celsius"], "T_max_celsius")
    if t_min >= t_max:
        raise ValueError("climate minimum temperature must be below maximum")
    h_min = _number(generation["temperature"]["min_hour"], "temperature.min_hour")
    h_max = _number(generation["temperature"]["max_hour"], "temperature.max_hour")
    if not 0 <= h_min < 24 or not 0 <= h_max < 24 or h_min == h_max:
        raise ValueError("temperature extreme hours must differ and lie in [0, 24)")
    warming = (h_max - h_min) % 24
    elapsed = (hour - h_min) % 24
    daily = np.where(elapsed <= warming, -np.cos(np.pi * elapsed / warming),
                     np.cos(np.pi * (elapsed - warming) / (24 - warming)))
    noise = _number(climate["noise_std"], "climate.noise_std")
    if noise < 0:
        raise ValueError("climate.noise_std must be nonnegative")
    temperature = np.clip(
        _number(climate["T_mean_annual"], "T_mean_annual")
        - _number(climate["seasonal_amplitude"], "seasonal_amplitude") * winter
        + _number(climate["diurnal_amplitude"], "diurnal_amplitude") * daily
        + rng.normal(0, noise, total_hours), t_min, t_max,
    )

    renewables = region["renewables"]
    pv_capacity = _number(renewables["pv_capacity_mw"], "pv_capacity_mw")
    wind_capacity = _number(renewables["wind_capacity_mw"], "wind_capacity_mw")
    if pv_capacity < 0 or wind_capacity < 0:
        raise ValueError("renewable capacities must be nonnegative")
    latitude = _number(region["latitude"], "latitude")
    if not -90 <= latitude <= 90:
        raise ValueError("latitude must lie in [-90, 90]")
    latitude = np.deg2rad(latitude)
    # Astronomical approximation, not a fitted coefficient: 23.44-degree tilt,
    # day 81 equinox. Hours are local solar time, since no time-zone is supplied.
    declination = np.deg2rad(23.44) * np.sin(2 * np.pi * (day - 81) / days_in_year)
    hour_angle = np.pi * (hour - 12) / 12
    solar_sine = np.sin(latitude) * np.sin(declination) + np.cos(latitude) * np.cos(declination) * np.cos(hour_angle)
    daylight = solar_sine > 0
    weather, clearness = _weather_indices(index, generation["solar"], rng)
    coefficient = generation["solar"]["temp_coefficient"]
    # Ambient temperature is the explicit module-temperature proxy: no NOCT
    # or panel thermal parameters are available. Clearness includes weather.
    temperature_gain = np.maximum(0, 1 + _number(coefficient, "temp_coefficient")
                                  * (temperature - _number(coefficient["reference_temp_celsius"], "reference_temp_celsius")))
    pv_fraction = np.clip(np.maximum(solar_sine, 0) * clearness * temperature_gain, 0, 1)
    pv_fraction[~daylight] = 0.0

    wind_cfg = generation["wind"]
    weibull = wind_cfg["weibull"]
    k = _number(weibull["k"], "wind.weibull.k")
    c = _number(weibull["c"], "wind.weibull.c")
    rho = _number(weibull["ar1_rho"], "wind.weibull.ar1_rho")
    if k <= 0 or c <= 0 or not 0 <= rho < 1:
        raise ValueError("Weibull k/c must be positive and AR rho must lie in [0, 1)")
    draws = c * rng.weibull(k, total_hours)
    speed = np.empty(total_hours)
    speed[0] = draws[0]
    for t in range(1, total_hours):
        speed[t] = rho * speed[t - 1] + (1 - rho) * draws[t]
    speed *= np.maximum(0, 1 + _number(weibull["seasonal_amplitude"], "wind.seasonal_amplitude") * winter)
    speed *= np.maximum(0, 1 + _number(weibull["diurnal_amplitude"], "wind.diurnal_amplitude")
                        * _number(renewables["wind_anticorrelation_strength"], "wind_anticorrelation_strength")
                        * np.cos(2 * np.pi * hour / 24))
    curve = wind_cfg["power_curve"]
    curve_v = np.asarray([_number(p["v"], "wind curve v") for p in curve["points"]])
    curve_p = np.asarray([_number(p["p"], "wind curve p") for p in curve["points"]])
    if len(curve_v) < 2 or not (np.diff(curve_v) > 0).all() or ((curve_p < 0) | (curve_p > 1)).any():
        raise ValueError("wind curve must have increasing speeds and fractions in [0, 1]")
    wind_fraction = np.interp(speed, curve_v, curve_p)
    wind_fraction[speed >= _number(curve["cut_out_v"], "wind.cut_out_v")] = 0

    electricity = {**economics["electricity"], **region.get("electricity_override", {})}
    price_buy = np.empty(total_hours)
    coverage = np.zeros(total_hours, dtype=int)
    for segment, bounds in electricity["segments"].items():
        start, end = map(float, bounds)
        if not 0 <= start < end <= 24:
            raise ValueError(f"electricity segment {segment} must lie within [0, 24]")
        key = f"price_{segment.rstrip('0123456789')}"
        selected = (hour >= start) & (hour < end)
        price_buy[selected] = _number(electricity[key], key)
        coverage[selected] += 1
    if not (coverage == 1).all():
        raise ValueError("electricity segments must cover each hour exactly once")
    carbon = {**economics["carbon"], **region.get("carbon_override", {})}
    ci_grid = _number(carbon["ci_grid_mid"], "ci_grid_mid")
    if ci_grid < 0:
        raise ValueError("ci_grid_mid must be nonnegative")
    compute = {**system["compute"], **region.get("compute_override", {})}
    total_gpu = _positive_integer(compute["gpu_capacity_total"], "gpu_capacity_total")
    fraction = _number(compute["s1_reserved_fraction"], "s1_reserved_fraction")
    if not 0 <= fraction <= 1:
        raise ValueError("s1_reserved_fraction must lie in [0, 1]")
    reserved_gpu = round(total_gpu * fraction)
    p_card = _number(compute["gpu_power"], "gpu_power")
    overhead = _number(compute["it_overhead_mw"], "it_overhead_mw")
    if p_card <= 0 or overhead < 0:
        raise ValueError("gpu_power must be positive and overhead nonnegative")
    it_base = reserved_gpu * p_card + overhead
    heat = region["heat"]
    heat_base = _number(heat["q_mun_demand_base_mw_th"], "heat base")
    heat_peak = _number(heat["q_mun_demand_peak_mw_th"], "heat peak")
    heat_capacity = _number(heat["q_ind_capacity_mw_th"], "industrial heat capacity")
    if heat_base < 0 or heat_peak < heat_base or heat_capacity < 0:
        raise ValueError("heat must be nonnegative and peak >= base")
    months = list(heat["heating_season_months"])
    if any(month not in range(1, 13) for month in months):
        raise ValueError("heating months must lie in [1, 12]")
    heat_demand = np.where(index.month.isin(months),
                           heat_base + (heat_peak - heat_base) * (t_max - temperature) / (t_max - t_min), heat_base)
    pue_points = system["pue"]["points"]
    pue_x = np.asarray([_number(p["T_amb"], "PUE temperature") for p in pue_points])
    pue_y = np.asarray([_number(p["value"], "PUE value") for p in pue_points])
    if len(pue_x) < 2 or not (np.diff(pue_x) > 0).all() or (pue_y < 1).any():
        raise ValueError("PUE temperatures must increase and values must be >= 1")
    site = {**system["storage_site_default"], **region.get("storage_site_override", {})}
    storage_temperature = temperature.copy()
    if not site["outdoor_installation"]:
        storage_temperature = np.clip(temperature + _number(site["indoor_temp_offset"], "indoor_temp_offset"),
                                      _number(site["indoor_temp_floor"], "indoor_temp_floor"),
                                      _number(site["indoor_temp_ceiling"], "indoor_temp_ceiling"))
    ts = pd.DataFrame({
        "T_amb": temperature, "P_pv_avail": pv_capacity * pv_fraction,
        "P_wind_avail": wind_capacity * wind_fraction, "price_buy": price_buy,
        "price_sell": _number(electricity["price_sell"], "price_sell"),
        "CI_grid": ci_grid, "IT_base": it_base, "Q_mun_demand": heat_demand,
        "PUE": np.interp(temperature, pue_x, pue_y), "T_storage": storage_temperature,
        "Q_ind_capacity": heat_capacity, "solar_elevation_sine": solar_sine,
        "is_daylight": daylight, "weather_state": weather,
        "clearness_index": clearness, "wind_speed_m_s": speed,
    }, index=index)
    ts.attrs = {
        "source": SIMULATION_NOTICE, "seed": int(seed), "region_id": region["region_id"],
        "year_hours": year_hours, "extra_hours": extra_hours, "total_hours": total_hours,
        "dt_hours": 1.0, "gpu_capacity_total": total_gpu,
        "gpu_capacity_s1_reserved": reserved_gpu, "gpu_capacity_plan": total_gpu - reserved_gpu,
        "gpu_power_mw_per_card": p_card, "it_base_mw": it_base,
        "generation_choices": {
            "temperature": "calendar-boundary winter cosine; configured daily extreme hours; climate clipping",
            "solar": "local solar time; horizontal elevation; 23.44 degree tilt; day 81 equinox; ambient module-temperature proxy",
            "weather": "daily Markov chain at stationary distribution; hourly configured clearness draws",
            "wind": "Weibull draws; configured AR(1) smoothing and seasonal/night modulation",
            "carbon": "constant configured regional mean; fluctuation amplitude unavailable",
            "heat": "seasonal base-to-peak demand linear in climate-normalized coldness",
        },
        "generation_sources": {"wind": curve.get("source"),
                               "solar": generation["solar"]["clearness_index"].get("source"),
                               "temperature": generation["temperature"].get("note")},
    }
    ts.attrs["capacity_factor_check"] = verify_capacity_factors(ts, region, pv_capacity, wind_capacity)
    return ts


def select_representative_weeks(
    year_ts: pd.DataFrame, low_fidelity_cfg: dict,
) -> tuple[list[pd.DataFrame], np.ndarray, list[str]]:
    """Select five independent continuous windows, returning configured weights.

    WINX starts exactly at the earliest annual temperature minimum. Typical
    weeks minimize equal-weight squared IQR-standardized distance of weekly
    mean T/PV/wind/heat to the seasonal median of candidate weekly means.
    Candidates stay fully in the meteorological season and annual segment;
    ties choose the earliest start. Original timestamps are preserved.
    """
    _validate_hourly(year_ts)
    hours = _positive_integer(low_fidelity_cfg["hours_per_week"], "hours_per_week")
    week_ids = list(low_fidelity_cfg["weeks"])
    seasons = {"W-WIN": (12, 1, 2), "W-SPR": (3, 4, 5), "W-SUM": (6, 7, 8), "W-AUT": (9, 10, 11)}
    if len(week_ids) != 5 or set(week_ids) != {*seasons, "W-WINX"}:
        raise ValueError("weeks must contain WIN/WINX/SPR/SUM/AUT exactly once")
    weights = np.asarray([_number(low_fidelity_cfg["weights"][w], w) for w in week_ids])
    if (weights < 0).any() or not np.isclose(weights.sum(), 1, rtol=0, atol=1e-9):
        raise ValueError("week weights must be nonnegative and sum to one")
    if "total_hours" in low_fidelity_cfg and _positive_integer(low_fidelity_cfg["total_hours"], "total_hours") != hours * len(week_ids):
        raise ValueError("low total_hours differs from weeks * hours_per_week")
    year_hours = _positive_integer(year_ts.attrs.get("year_hours", 8760), "year_hours")
    if len(year_ts) < year_hours:
        raise ValueError("selection requires the complete annual segment")
    features = ["T_amb", "P_pv_avail", "P_wind_avail", "Q_mun_demand"]
    annual = year_ts.iloc[:year_hours]
    if not np.isfinite(annual[features].to_numpy(dtype=float)).all():
        raise ValueError("representative-week features must be finite")
    weekly_means = annual[features].rolling(hours).mean().shift(-(hours - 1))
    weeks = []
    for week_id, weight in zip(week_ids, weights, strict=True):
        if week_id == "W-WINX":
            start = int(np.argmin(annual["T_amb"].to_numpy()))
            score = None
            rule = "annual minimum T_amb and following hours; no shift or wrap"
        else:
            in_season = annual.index.month.isin(seasons[week_id])
            valid = pd.Series(in_season).rolling(hours).sum().shift(-(hours - 1)).to_numpy() == hours
            candidates = np.flatnonzero(valid)
            if not len(candidates):
                raise ValueError(f"no complete {hours}-hour candidate for {week_id}")
            values = weekly_means.iloc[candidates].to_numpy()
            center = np.median(values, axis=0)
            spread = np.quantile(values, .75, axis=0) - np.quantile(values, .25, axis=0)
            spread = np.where(spread > 0, spread, 1)
            scores = np.square((values - center) / spread).sum(axis=1)
            chosen = int(np.argmin(scores))
            start, score = int(candidates[chosen]), float(scores[chosen])
            rule = "minimum standardized distance to seasonal median weekly mean T/PV/wind/heat"
        if start + hours > len(year_ts):
            raise ValueError(f"{week_id} window exceeds data; refusing to shift or wrap")
        week = year_ts.iloc[start:start + hours].copy()
        week.attrs.update({"week_id": week_id, "weight": float(weight), "start_position": start,
                           "selection_rule": rule, "selection_score": score, "independent_window": True})
        weeks.append(week)
    return weeks, weights, week_ids


def verify_capacity_factors(
    ts: pd.DataFrame, region_cfg: dict, capacity_pv_mw: float,
    capacity_wind_mw: float, tolerance: float = 0.05,
) -> dict:
    """Check the annual segment only, warning without fitting the generator.

    Zero capacity yields undefined CF (NaN), explicitly skipped; nonzero output
    with zero capacity is an input error. Short windows use their actual length.
    """
    _validate_hourly(ts)
    tolerance = _number(tolerance, "CF tolerance")
    if tolerance < 0:
        raise ValueError("CF tolerance must be nonnegative")
    annual_hours = min(len(ts), _positive_integer(ts.attrs.get("year_hours", 8760), "year_hours"))
    annual = ts.iloc[:annual_hours]
    result: dict = {"annual_hours_used": annual_hours, "tolerance": tolerance}
    passed = True
    for kind, capacity in (("pv", capacity_pv_mw), ("wind", capacity_wind_mw)):
        capacity = _number(capacity, f"capacity_{kind}_mw")
        values = annual[f"P_{kind}_avail"].to_numpy(dtype=float)
        if capacity < 0 or not np.isfinite(values).all() or (values < 0).any():
            raise ValueError("renewable capacities/outputs must be finite and nonnegative")
        reference = _number(region_cfg["renewables"][f"cf_{kind}"], f"cf_{kind}")
        if not 0 <= reference <= 1:
            raise ValueError("CF references must lie in [0, 1]")
        if capacity == 0:
            if np.any(values != 0):
                raise ValueError(f"{kind} has nonzero output but zero capacity")
            actual = deviation = float("nan")
            result[f"cf_{kind}_skipped"] = True
        else:
            actual = float(values.sum() / (capacity * annual_hours))
            deviation = actual - reference
            result[f"cf_{kind}_skipped"] = False
            if abs(deviation) > tolerance:
                passed = False
                warnings.warn(f"{region_cfg.get('region_id', 'region')} {kind} simulated CF {actual:.6f} "
                              f"differs from reference {reference:.6f} by {deviation:+.6f} "
                              f"(tolerance {tolerance}); generation parameters were not adjusted.",
                              UserWarning, stacklevel=2)
        result.update({f"cf_{kind}_actual": actual, f"cf_{kind}_reference": reference,
                       f"cf_{kind}_deviation": deviation})
    result["passed"] = passed
    return result


def build_timeseries(
    region: str, fidelity: Fidelity, config_dir: Path, seed: int,
) -> tuple[pd.DataFrame, np.ndarray]:
    """High returns 8760 hours/hour weights; low returns 840 hours/5 week weights.

    Dispatch low-fidelity blocks independently; block offsets and IDs are in
    attrs and a column. Timestamps may repeat when independently chosen weeks
    overlap. They must never imply a continuous SOC trajectory across weeks.
    """
    from aidc.data.chemistry import load_config_bundle

    if fidelity not in ("low", "high"):
        raise ValueError("fidelity must be 'low' or 'high'")
    bundle = load_config_bundle(Path(config_dir))
    if region not in bundle["regions"]:
        raise ValueError(f"unknown region {region!r}")
    ts = build_year_timeseries(bundle["regions"][region], bundle["system"],
                               bundle["economics"], bundle["generation"], seed)
    if fidelity == "high":
        annual = ts.iloc[:ts.attrs["year_hours"]].copy()
        annual.attrs["fidelity"] = "high"
        return annual, np.full(len(annual), 1 / len(annual))
    weeks, weights, week_ids = select_representative_weeks(ts, bundle["experiments"]["fidelity"]["low"])
    blocks, metadata = [], []
    offset = 0
    for week, week_id, weight in zip(weeks, week_ids, weights, strict=True):
        block = week.copy()
        block["representative_week_id"] = week_id
        block.attrs = {}
        blocks.append(block)
        metadata.append({"week_id": week_id, "start_position": offset, "hours": len(week),
                         "timestamp_start": week.index[0].isoformat(), "weight": float(weight)})
        offset += len(week)
    result = pd.concat(blocks)
    result.attrs = {**ts.attrs, "fidelity": "low", "representative_week_blocks": metadata,
                    "independent_windows": True, "week_weights": weights.tolist()}
    return result, weights


def slice_window(ts: pd.DataFrame, start: pd.Timestamp, hours: int) -> pd.DataFrame:
    """Copy an exact continuous window; reject absent starts or truncation."""
    _validate_hourly(ts)
    count = _positive_integer(hours, "hours")
    timestamp = pd.Timestamp(start)
    if timestamp not in ts.index:
        raise ValueError(f"window start {timestamp} is absent")
    position = int(ts.index.get_loc(timestamp))
    if position + count > len(ts):
        raise ValueError("requested window extends beyond available data")
    result = ts.iloc[position:position + count].copy()
    result.attrs.update({"window_start": timestamp.isoformat(), "window_hours": count})
    return result


def main(argv: list[str] | None = None) -> int:
    """Print four-region M1 diagnostics without writing generated artifacts."""
    from aidc.data.chemistry import load_config_bundle

    parser = argparse.ArgumentParser(description=SIMULATION_NOTICE)
    parser.add_argument("--check-all-regions", action="store_true")
    parser.add_argument("--config-dir", type=Path, default=Path(__file__).resolve().parents[3] / "configs")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--year", type=int, default=2026)
    args = parser.parse_args(argv)
    if not args.check_all_regions:
        parser.error("specify --check-all-regions")
    bundle = load_config_bundle(args.config_dir)
    seed = args.seed if args.seed is not None else int(bundle["experiments"]["seeds"][0])
    print(SIMULATION_NOTICE)
    for region_id, region in bundle["regions"].items():
        ts = build_year_timeseries(region, bundle["system"], bundle["economics"], bundle["generation"], seed, args.year)
        weeks, weights, week_ids = select_representative_weeks(ts, bundle["experiments"]["fidelity"]["low"])
        annual = ts.iloc[:ts.attrs["year_hours"]]
        result = {"region": region_id, "seed": seed, "hours": len(ts), "annual_hours": len(annual),
                  "T_min": float(annual.T_amb.min()), "T_max": float(annual.T_amb.max()),
                  "IT_base_mw": ts.attrs["it_base_mw"], "E_S1_annual_mwh": float(annual.IT_base.sum()),
                  "night_pv_max_mw": float(ts.loc[~ts.is_daylight, "P_pv_avail"].max()),
                  "capacity_factors": ts.attrs["capacity_factor_check"],
                  "weeks": [{"id": name, "start": week.index[0].isoformat(), "hours": len(week), "weight": float(weight)}
                            for week, weight, name in zip(weeks, weights, week_ids, strict=True)],
                  "choices": ts.attrs["generation_choices"]}
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
