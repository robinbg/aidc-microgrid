"""Deterministic fault scenarios and their audit table (SPEC §5.1).

Probabilities are normalized simulation weights, not calibrated event
frequencies. Renewable targets use separate empirical clock-hour percentiles
for PV and wind, never a single annual minimum.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
import pandas as pd

ScenarioType = Literal[
    "grid_outage", "grid_outage_extended", "renewable_drop",
    "renewable_drop_plus_grid", "n1_storage", "n1_plus_grid",
]
_GRID_TYPES = {"grid_outage", "grid_outage_extended", "renewable_drop_plus_grid", "n1_plus_grid"}


@dataclass
class FaultScenario:
    """A fault interval indexed by hourly positions in the supplied trace.

    renewable_*_mw contain absolute targets for the fault interval. Apply
    min(original, target) to prevent raising an already low realization.
    renewable_scale is retained for manually constructed scaled scenarios;
    generated scenarios use profiles, including when the PV base is zero.
    """
    scenario_id: str
    type: ScenarioType
    start_h: int
    duration_h: int
    outage_hours: int | None = None
    renewable_scale: float | None = None
    failed_storage: str | None = None
    probability: float = 1.0
    description: str = ""
    reports_t_island: bool = False
    counts_toward_n1_pass: bool = False
    renewable_pv_mw: tuple[float, ...] | None = None
    renewable_wind_mw: tuple[float, ...] | None = None
    renewable_reference: str = ""
    sampling_source: str = "manual"


def _resilience_config(cfg: dict) -> dict:
    """Accept a resilience section, economics document or loaded bundle."""
    if not isinstance(cfg, dict):
        raise ValueError("cfg must be a configuration dictionary")
    if "economics" in cfg:
        cfg = cfg["economics"]
    elif "econ" in cfg:
        cfg = cfg["econ"]
    if "resilience" in cfg:
        cfg = cfg["resilience"]
    if not isinstance(cfg, dict):
        raise ValueError("resilience configuration must be a dictionary")
    return cfg


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if not np.isfinite(number) or number <= 0 or number != int(number):
        raise ValueError(f"{name} must be a positive integer")
    return int(number)


def _durations(cfg: dict, key: str) -> list[int]:
    if key not in cfg or not isinstance(cfg[key], (list, tuple)) or not cfg[key]:
        raise ValueError(f"{key} must be a nonempty duration list")
    result = [_positive_integer(value, key) for value in cfg[key]]
    if len(set(result)) != len(result):
        raise ValueError(f"{key} must not contain duplicate durations")
    return result


def _stratified_starts(index, duration, count, rng) -> list[int]:
    """One draw per equal clock-hour stratum; never wrap fault intervals."""
    if duration > len(index):
        raise ValueError(f"fault duration {duration} exceeds trace length {len(index)}")
    if count > 24:
        raise ValueError("n_samples_per_outage must be at most 24 for hourly stratification")
    positions = np.arange(len(index) - duration + 1)
    hours = index.hour.to_numpy()[positions]
    result = []
    for stratum in np.array_split(np.arange(24), count):
        candidates = positions[np.isin(hours, stratum)]
        if not len(candidates):
            raise ValueError(f"no legal {duration} h start in clock-hour stratum {stratum.tolist()}; provide a longer trace")
        result.append(int(rng.choice(candidates)))
    return result


def build_scenarios(ts: pd.DataFrame, cfg: dict, seed: int) -> list[FaultScenario]:
    """Generate six fault types, preserving input data and configuration.

    Config accepts a resilience section, economics dictionary or loaded bundle.
    Input has continuous hourly DatetimeIndex and finite nonnegative renewable
    MW values. Too-short traces are rejected, never wrapped or truncated.

    Simulation choices, made explicit for reproducibility:
      * Each grid/drop duration has n_samples draws from equal clock-hour bins.
      * R-drop+G reuses grid intervals of exactly the drop duration. Missing
        matching grid duration raises rather than inventing another duration.
      * Standalone Na/Li N-1 failures share one randomly sampled observation
        interval. N-1+G reuses every grid interval for each chemistry.
      * All scenarios have weight 1/N, not estimated event frequencies. Zero
        capacity failures remain here (no sizing input); evaluation records
        skips and aggregation renormalizes weights over applied scenarios.
      * Percentiles use the supplied history, independently by clock hour and
        technology. Evaluation applies min(original, target), never raises it.

    Defaults yield 82 scenarios: 20 grid, 10 renewable, 10 renewable+grid,
    2 N-1 and 40 N-1+grid. There is no annual extrapolation of these weights.
    """
    rcfg = _resilience_config(cfg)
    if not isinstance(ts, pd.DataFrame) or not isinstance(ts.index, pd.DatetimeIndex):
        raise ValueError("ts must be a DataFrame with a DatetimeIndex")
    if len(ts) == 0 or ts.index.hasnans or not ts.index.is_unique:
        raise ValueError("ts index must be nonempty, unique and contain no NaT")
    if len(ts) > 1 and not np.all(np.diff(ts.index.asi8) == pd.Timedelta(hours=1).value):
        raise ValueError("ts must have a continuous increasing hourly index")
    for column in ("P_pv_avail", "P_wind_avail"):
        if column not in ts:
            raise ValueError(f"ts is missing {column}")
        values = np.asarray(ts[column], dtype=float)
        if not np.all(np.isfinite(values)) or np.any(values < 0):
            raise ValueError(f"{column} must contain finite nonnegative MW values")
    regular = _durations(rcfg, "grid_outage_hours")
    extended = _durations(rcfg, "grid_outage_extended_hours")
    drops = _durations(rcfg, "renewable_drop_hours")
    if set(regular) & set(extended):
        raise ValueError("regular and extended grid durations must not overlap")
    count = _positive_integer(rcfg.get("n_samples_per_outage"), "n_samples_per_outage")
    try:
        percentile = float(rcfg["renewable_drop_percentile"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("renewable_drop_percentile is required") from exc
    if not np.isfinite(percentile) or not 0 <= percentile <= 100:
        raise ValueError("renewable_drop_percentile must lie in [0, 100]")
    combined = rcfg.get("combined_scenarios", {})
    if not isinstance(combined, dict):
        raise ValueError("combined_scenarios must be a dictionary")
    for name, value in (("n1_storage_failure", rcfg.get("n1_storage_failure")), *combined.items()):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
    if combined.get("renewable_drop_plus_grid_outage", False):
        missing = set(drops) - set(regular + extended)
        if missing:
            raise ValueError(f"R-drop + G needs matching configured grid durations: {sorted(missing)}")
    rng = np.random.default_rng(seed)
    result = []
    grid_intervals = {}
    source = "equal_weight_deterministic_clock_hour_strata"
    grid_types = [(duration, "grid_outage") for duration in regular]
    grid_types += [(duration, "grid_outage_extended") for duration in extended]
    for duration, scenario_type in grid_types:
        starts = _stratified_starts(ts.index, duration, count, rng)
        grid_intervals[duration] = starts
        for sample, start in enumerate(starts):
            result.append(FaultScenario(
                f"G{duration}-{sample + 1:02d}", scenario_type, start, duration,
                outage_hours=duration, reports_t_island=True,
                description=f"Grid import/export unavailable for {duration} h",
                sampling_source=source,
            ))
    hourly = ts[["P_pv_avail", "P_wind_avail"]].groupby(ts.index.hour).quantile(percentile / 100)

    def renewable_scenario(start, duration, sample, grid):
        targets = hourly.reindex(ts.index[start:start + duration].hour)
        return FaultScenario(
            f"R-drop{'-G' if grid else ''}{duration}-{sample + 1:02d}",
            "renewable_drop_plus_grid" if grid else "renewable_drop", start, duration,
            outage_hours=duration if grid else None, reports_t_island=grid,
            renewable_pv_mw=tuple(float(v) for v in targets["P_pv_avail"]),
            renewable_wind_mw=tuple(float(v) for v in targets["P_wind_avail"]),
            renewable_reference=f"hour_of_day_empirical_percentile:{percentile:g}",
            sampling_source=source,
            description=f"PV and wind capped at separate clock-hour {percentile:g}th percentiles"
                        + ("; grid import/export unavailable" if grid else ""),
        )

    for duration in drops:
        for sample, start in enumerate(_stratified_starts(ts.index, duration, count, rng)):
            result.append(renewable_scenario(start, duration, sample, False))
        if combined.get("renewable_drop_plus_grid_outage", False):
            for sample, start in enumerate(grid_intervals[duration]):
                result.append(renewable_scenario(start, duration, sample, True))
    if rcfg["n1_storage_failure"]:
        duration = _positive_integer(rcfg.get("n1_observation_hours"), "n1_observation_hours")
        if duration > len(ts):
            raise ValueError("N-1 observation interval exceeds trace length")
        start = int(rng.integers(0, len(ts) - duration + 1))
        for chemistry in ("Na", "Li"):
            result.append(FaultScenario(
                f"N-1-{chemistry}", "n1_storage", start, duration,
                failed_storage=chemistry, counts_toward_n1_pass=True,
                description=f"{chemistry} storage unavailable; grid remains connected",
                sampling_source="equal_weight_deterministic_paired_n1_interval",
            ))
        if combined.get("n1_plus_grid_outage", False):
            for duration, starts in grid_intervals.items():
                for sample, start in enumerate(starts):
                    for chemistry in ("Na", "Li"):
                        result.append(FaultScenario(
                            f"N-1-{chemistry}-G{duration}-{sample + 1:02d}", "n1_plus_grid",
                            start, duration, outage_hours=duration, failed_storage=chemistry,
                            reports_t_island=True,
                            description=f"{chemistry} storage and grid unavailable for {duration} h",
                            sampling_source=source,
                        ))
    for scenario in result:
        scenario.probability = 1.0 / len(result)
    return result


def scenarios_summary(scenarios: list[FaultScenario]) -> pd.DataFrame:
    """Return all flags, weights, sampling sources and complete target profiles.

    Profiles remain tuples for traceability. An empty list preserves the schema.
    """
    columns = list(FaultScenario.__dataclass_fields__) + ["cuts_grid", "end_h"]
    rows = []
    for scenario in scenarios:
        row = asdict(scenario)
        row.update(cuts_grid=scenario.type in _GRID_TYPES, end_h=scenario.start_h + scenario.duration_h)
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)
