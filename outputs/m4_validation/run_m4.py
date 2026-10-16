"""M4 生产运行：4 臂 × 5 策略 × 5 种子 = 100 组（低保真代表周路径，BT2 裁定）。

流程：
  1. 哈希核对：11 份 YAML 与 _baseline.py 必须与 v2.10 重取基线一致
     （outputs/m4_validation/protected_baseline.json）；dispatch.py 等实现
     模块只做**运行期间稳定性**核对（v2.10 守卫实现后其哈希与基线不同
     是预期的——基线采集于守卫实现前，CHANGELOG「哈希基线必须重取」）。
  2. run_matrix 并行跑完 100 组（断点续跑：已有完整记录的组直接加载）。
  3. 不变量：每组 5 个周窗全部通过 §9 轨迹级核查，任何失败都会列出。
  4. 导出主对照表（CSV + Markdown，含 §10.6 固定定义与全部口径注记）。
  5. report.json：状态计数、守卫触发清单、BT2-③ 临时结论清单、f3 政策、
     耗时与哈希。

用法：
    python -B outputs/m4_validation/run_m4.py [workers=6]
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from aidc.experiments.run_matrix import run_matrix  # noqa: E402
from aidc.report.tables import export_master_table  # noqa: E402

FROZEN_STRICT = None  # 从 protected_baseline.json 读取（YAML + _baseline.py 严格一致）


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, pd.DataFrame):
        return "[dataframe omitted]"
    if isinstance(value, (np.ndarray, list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_hashes() -> dict:
    hashes = {p.relative_to(ROOT).as_posix(): sha(p)
              for p in sorted((ROOT / "configs").rglob("*.yaml"))}
    for rel in ("src/aidc/model/_baseline.py", "src/aidc/model/dispatch.py"):
        hashes[rel] = sha(ROOT / rel)
    return hashes


def main() -> None:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    OUT.mkdir(parents=True, exist_ok=True)
    baseline = json.loads((OUT / "protected_baseline.json").read_text(encoding="utf-8"))["sha256"]
    before = current_hashes()

    strict_keys = [k for k in baseline if k != "src/aidc/model/dispatch.py"]
    mismatch = [k for k in strict_keys if before.get(k) != baseline[k]]
    assert not mismatch, f"受保护文件与 v2.10 基线不一致: {mismatch}"
    dispatch_note = ("dispatch.py 哈希与基线不同属预期：基线采集于 v2.10 归一化"
                     "越界守卫实现之前（CHANGELOG「哈希基线必须重取」+ §3.5 守卫为"
                     " v2.10 规范性要求）") if before["src/aidc/model/dispatch.py"] != baseline.get("src/aidc/model/dispatch.py") else "dispatch.py 与基线一致"

    print(f"START M4 matrix: 4 arms x 5 strategies x 5 seeds, {workers} workers", flush=True)
    started = time.perf_counter()
    df = run_matrix("R-NW", ROOT / "configs", OUT, parallel=True, n_workers=workers)
    matrix_elapsed = time.perf_counter() - started
    print(f"MATRIX DONE: {len(df)} combos in {matrix_elapsed:.0f}s", flush=True)

    csv_path, md_path = export_master_table(df, OUT)

    after = current_hashes()
    stable = before == after
    strict_after = [k for k in strict_keys if after.get(k) != baseline[k]]

    n_infeasible = int((df["status"] != "feasible").sum())
    invariants_ok = bool(df["invariants_all_ok"].all())
    guard_combos = df.loc[df["s_balance_guard_fired"] == True,  # noqa: E712
                          ["arm", "strategy", "seed"]].to_dict("records")
    provisional = df.loc[df["provisional_for_comparison"] == True,  # noqa: E712
                         ["arm", "strategy", "seed", "max_certified_gap"]].to_dict("records")
    report = {
        "stage": "M4-matrix", "spec_version": "2.10", "simulation": True,
        "path_choice": "低保真代表周（5×168h，BT2 裁定；§6.3 要求写明）",
        "n_combos": int(len(df)),
        "status_counts": df["status"].value_counts().to_dict(),
        "n_infeasible_recorded_not_skipped": n_infeasible,
        "invariants_all_ok": invariants_ok,
        "s_balance_guard_fired_combos": guard_combos,
        "provisional_gap_over_2pct_combos": provisional,
        "f3_policy": ("resilience_gap 全为 NaN（低保真，§4.2）；臂级韧性列来自 M2.5 场景包，"
                      "全部右删失 → NON-DISCRIMINATING，不参与任何排序"),
        "green_rate_scope": "分母含充电、不追溯储能来源；不可跨储能规模直接对比",
        "s_base_note": "规则驱动，objective 为 NaN 属契约；BS3 政策性弃电不对称已注记",
        "investment_note": "A1/A2/A3 统一 150 MWh / 45 MW；A0 投资 0；跨臂比较须携带投资额",
        "matrix_elapsed_s": matrix_elapsed,
        "total_elapsed_s": time.perf_counter() - started,
        "sources_stable_during_run": stable,
        "strict_frozen_unchanged": not strict_after,
        "dispatch_note": dispatch_note,
        "hashes_before": before, "hashes_after": after,
        "master_table": {"csv": str(csv_path.relative_to(ROOT)),
                         "md": str(md_path.relative_to(ROOT))},
        "columns": list(df.columns),
    }
    (OUT / "report.json").write_text(json.dumps(clean(report), ensure_ascii=False,
                                                indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(clean({k: report[k] for k in
                            ("n_combos", "status_counts", "invariants_all_ok",
                             "s_balance_guard_fired_combos", "provisional_gap_over_2pct_combos",
                             "sources_stable_during_run", "strict_frozen_unchanged",
                             "matrix_elapsed_s")}),
                     ensure_ascii=False, indent=2), flush=True)
    assert stable, "sources changed during the run; rerun on frozen code"
    assert not strict_after, f"frozen files changed: {strict_after}"
    assert invariants_ok, "invariant violations in matrix results"
    assert len(df) == 100, f"expected 100 combos, got {len(df)}"


if __name__ == "__main__":
    main()
