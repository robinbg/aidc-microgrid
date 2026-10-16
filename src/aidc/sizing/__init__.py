"""外层容量配置：目标函数、贝叶斯优化、网格扫描对照。"""

from aidc.sizing.bo import BOResult, compute_hypervolume, filter_pareto, run_bo
from aidc.sizing.grid_search import build_grid, compare_bo_vs_grid, run_grid_search
from aidc.sizing.objective import (
    capital_recovery_factor,
    compute_lcoe,
    compute_resilience_gap,
    evaluate_sizing,
    storage_annualized_cost,
    to_objective_vector,
)

__all__ = [
    "BOResult",
    "build_grid",
    "capital_recovery_factor",
    "compare_bo_vs_grid",
    "compute_hypervolume",
    "compute_lcoe",
    "compute_resilience_gap",
    "evaluate_sizing",
    "filter_pareto",
    "run_bo",
    "run_grid_search",
    "storage_annualized_cost",
    "to_objective_vector",
]
