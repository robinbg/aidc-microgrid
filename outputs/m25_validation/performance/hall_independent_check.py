"""Independent finite exhaustive check of the proposed interval-Hall model.

Run from any directory with the project Python runtime. No production code is
imported. This checks the finite domain H=4,N=3 exhaustively; the general result
still rests on the interval bipartite matching argument, not finite testing.
It also counts candidate Hall rows for the saved 168 h, 2200-job M2 input.
"""
from __future__ import annotations

from hashlib import sha256
from itertools import combinations_with_replacement, product
import json
from pathlib import Path
import platform
from time import perf_counter

import numpy as np


def hall_rows(windows, horizon):
    """Return integer RHS table and three equivalent candidate row lists."""
    histogram = np.zeros((horizon, horizon), dtype=int)
    for earliest, latest in windows:
        if not 0 <= earliest <= latest < horizon:
            raise ValueError("windows must be nonempty contiguous integer intervals")
        histogram[earliest, latest] += 1
    # K[a,b] = count of jobs with earliest >= a and latest <= b.
    rhs = histogram[::-1].cumsum(0)[::-1].cumsum(1)
    releases = sorted({a for a, _ in windows})
    deadlines = sorted({b for _, b in windows})
    endpoints = [(a, b) for a in releases for b in deadlines if a <= b and rhs[a, b] > 0]
    # A non-tight hull has the same contained jobs on a smaller interval and
    # is dominated by that smaller interval because counts are nonnegative.
    tight = [(a, b) for a, b in endpoints
             if rhs[a, b] > (rhs[a + 1, b] if a + 1 < horizon else 0)
             and rhs[a, b] > (rhs[a, b - 1] if b > 0 else 0)]
    # If no contained job crosses a cut, the interval row is the sum of its
    # left/right interval rows. Recursive omissions terminate on shorter rows.
    indecomposable = [(a, b) for a, b in tight
                     if not any(rhs[a, cut] + rhs[cut + 1, b] == rhs[a, b]
                                for cut in range(a, b))]
    return rhs, {"endpoint": endpoints, "tight_hull": tight,
                 "indecomposable": indecomposable}


def exhaustive_check():
    horizon, n_jobs = 4, 3
    possible_windows = [(a, b) for a in range(horizon) for b in range(a, horizon)]
    capacities = [np.bincount(positions, minlength=horizon)
                  for positions in combinations_with_replacement(range(horizon), n_jobs)]
    groups, checks, matched = 0, 0, {"endpoint": 0, "tight_hull": 0, "indecomposable": 0}
    digest = sha256()
    for windows in combinations_with_replacement(possible_windows, n_jobs):
        groups += 1
        rhs, row_sets = hall_rows(windows, horizon)
        # Ground truth is direct enumeration of each labelled job's allowed
        # start. It does not use Hall inequalities or a matching solver.
        reachable = {tuple(np.bincount(starts, minlength=horizon))
                     for starts in product(*(range(a, b + 1) for a, b in windows))}
        for counts in capacities:
            checks += 1
            expected = tuple(counts) in reachable
            for label, rows in row_sets.items():
                actual = all(counts[a:b + 1].sum() >= rhs[a, b] for a, b in rows)
                if actual != expected:
                    raise AssertionError({"windows": windows, "counts": counts.tolist(),
                                          "rows": rows, "regime": label,
                                          "expected": expected, "actual": actual})
                matched[label] += 1
            digest.update(json.dumps([windows, counts.tolist(), expected], separators=(",", ":")).encode())
    return {"horizon": horizon, "jobs_per_group": n_jobs,
            "possible_windows": len(possible_windows), "window_multisets": groups,
            "count_distributions_per_group": len(capacities), "checks": checks,
            "passed_by_row_regime": matched, "case_digest_sha256": digest.hexdigest()}


def saved_workload_counts(workspace):
    """Count rows on the real saved task input after all late edges are pruned."""
    path = workspace / "outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv"
    if not path.is_file():
        return {"available": False, "path": str(path)}
    import pandas as pd
    table = pd.read_csv(path)
    horizon = 168
    totals = {"endpoint": 0, "tight_hull": 0, "indecomposable": 0}
    groups = []
    for (gpu, duration), group in table.groupby(["gpu", "duration_h"]):
        windows = list(zip(group.arrive_h.astype(int), (group.deadline_h - duration).astype(int)))
        _, rows = hall_rows(windows, horizon)
        row_counts = {name: len(value) for name, value in rows.items()}
        groups.append({"gpu": int(gpu), "duration_h": int(duration), "jobs": len(group), **row_counts})
        for name, count in row_counts.items():
            totals[name] += count
    return {"available": True, "path": str(path), "input_sha256": sha256(path.read_bytes()).hexdigest(),
            "horizon": horizon, "jobs": len(table), "groups": len(groups),
            "hall_row_totals": totals, "by_group": groups,
            "condition": "All late candidates removed; allowed start interval [arrive, deadline-duration]."}


def main():
    started = perf_counter()
    script = Path(__file__).resolve()
    report = {"status": "passed", "python": platform.python_version(),
              "numpy": np.__version__, "script_sha256": sha256(script.read_bytes()).hexdigest(),
              "exhaustive_finite_check": exhaustive_check(),
              "saved_workload": saved_workload_counts(script.parents[3]),
              "scope": "Independent finite validation; imports no production dispatch implementation.",
              "general_prerequisites": [
                  "Each job has a nonempty contiguous allowed integer start interval.",
                  "Counts are nonnegative integers and sum to the number of jobs in that group.",
                  "Only GPU count and duration affect physical constraints within a group.",
                  "All per-job late terms are absent or identically zero before aggregation.",
                  "Deterministic integer matching reconstructs actual jobs and metrics."],
              "elapsed_s": perf_counter() - started}
    target = script.with_suffix(".json")
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": report["status"],
                      "checks": report["exhaustive_finite_check"]["checks"],
                      "saved_workload_rows": report["saved_workload"].get("hall_row_totals"),
                      "report": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
