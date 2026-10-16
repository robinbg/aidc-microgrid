"""结果导出：表格与图表。

所有导出内容必须遵守 SPEC §10 的表述红线。
"""

from aidc.report.tables import (
    DISCLAIMER,
    REQUIRED_DEFINITIONS,
    export_master_table,
    export_regional_table,
    export_sensitivity_boundaries,
    plot_bo_vs_grid,
    plot_dispatch_trajectories,
    plot_pareto_front,
    plot_resilience,
    plot_storage_derating_comparison,
)

__all__ = [
    "DISCLAIMER",
    "REQUIRED_DEFINITIONS",
    "export_master_table",
    "export_regional_table",
    "export_sensitivity_boundaries",
    "plot_bo_vs_grid",
    "plot_dispatch_trajectories",
    "plot_pareto_front",
    "plot_resilience",
    "plot_storage_derating_comparison",
]
