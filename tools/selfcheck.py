#!/usr/bin/env python3
"""交付前自查：规格一致性检查（SPEC v2.9）。

**为什么需要这个工具**：本项目的规格经过五轮复核，每轮都发现 4–9 条问题，
其中大量是**跨文件一致性**问题（同一件事在两处写法不同、改了一处忘了另一处、
引用指向已作废的文件）。这些靠人眼逐行看极易漏，靠机器一秒可得。

**不检查什么**：不检查物理/数学正确性，也不检查测试的语义是否正确。
它只检查"同一件事是否说法一致""引用是否存在""编号是否唯一"。
物理正确性仍需人工复核（历史证明这是主要错误来源）。

用法：
    python tools/selfcheck.py            # 只报告
    python tools/selfcheck.py --strict   # 有 ERROR 时退出码 1

约定：ERROR = 必然导致实现错误或读错文件；WARN = 可疑，需人工确认。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "SPEC.md"
CHANGELOG = ROOT / "CHANGELOG.md"
README = ROOT / "README.md"
PROMPT = ROOT / "PROMPT.md"
CONFIGS = ROOT / "configs"
SRC = ROOT / "src"
TESTS = ROOT / "tests"

# 历史留档：**允许**出现 §U/§V/§W/§Z 这类指向它们的旧编号
ARCHIVE_OK = {CHANGELOG.name, "reply.txt"}
ARCHIVE_GLOB = "SPEC-REVISIONS"

errors: list[str] = []
warns: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warns.append(msg)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def live_files() -> list[Path]:
    """所有"实现时会被读到"的文件（排除历史留档与工具自身）。"""
    out: list[Path] = []
    for pat in ("*.md", "*.yaml", "*.py", "*.toml", "*.txt"):
        for p in ROOT.rglob(pat):
            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            if ARCHIVE_GLOB in rel or "__pycache__" in rel:
                continue
            if rel.startswith(".venv") or rel.startswith("tools/"):
                continue
            out.append(p)
    return out


# ---------------------------------------------------------------- 1. 引用完整性

def check_dangling_refs() -> None:
    """禁止在非留档文件中**指向**已作废的历史附录。

    ⚠️ 例外：**警告性提及**是允许的——"不要读 `SPEC-REVISIONS*.md`"
    这类句子必须出现（它正是防止误读的机制）。
    判据：同一行是否含"不要读 / 历史留档 / 已作废 / 不要"等标记。
    """
    pat = re.compile(r"§[UVWXYZ]\d|§A[EFGHIJ]\b|SPEC-REVISIONS")
    allow_ctx = ("不要读", "不要再去读", "历史留档", "已作废", "历史附录",
                 "均为历史", "不要让它去读", "不要", "含作废")
    for p in live_files():
        if p.name in ARCHIVE_OK:
            continue
        for i, line in enumerate(read(p).splitlines(), 1):
            if pat.search(line) and not any(a in line for a in allow_ctx):
                err(f"{p.relative_to(ROOT)}:{i} 指向已作废的历史附录编号，"
                    f"且无警告性说明：{line.strip()[:90]}")


def check_section_refs() -> None:
    """SPEC 内 §x.y 引用必须存在，且 §9.x 必须落在有效区间。

    ⚠️ **`§9.x` 的合法集合 = 标题 + 表格行**。
    测试条目以表格形式登记（`| 9.12 | ...`），不是 `### 9.12` 标题；
    若只认标题，`§9.1–§9.36` 这类引用会被全部误判为"不存在"。
    """
    spec = read(SPEC)
    lines = spec.splitlines()

    known: set[str] = set()
    for line in lines:
        m = re.match(r"^#{2,4}\s+(\d+(?:\.\d+)?)", line)
        if m:
            known.add(m.group(1))                 # 普通小节
        m2 = re.match(r"^\|\s*(9\.\d+)\s*\|", line)
        if m2:
            known.add(m2.group(1))                # 测试条目表格行
        m3 = re.match(r"^###\s+(9\.\d+)\s", line)
        if m3:
            known.add(m3.group(1))                # 规则小节

    for i, line in enumerate(lines, 1):
        for ref in re.findall(r"§(\d+(?:\.\d+)?)", line):
            if "." not in ref or ref in known:
                continue
            if ref.startswith("9."):
                err(f"SPEC.md:{i} 引用了不存在的测试/规则节 §{ref}")
            else:
                warn(f"SPEC.md:{i} 引用 §{ref} 但未找到同名标题（可能是简写）")
        for ref in re.findall(r"§9\.(\d+)", line):
            n = int(ref)
            if not (1 <= n <= 39):
                err(f"SPEC.md:{i} §9.{n} 越界（有效范围 9.1–9.39）")


# ---------------------------------------------------------------- 2. 编号唯一性

def check_test_numbering() -> None:
    """§9 的测试条目编号必须唯一，规则节不得与测试条目重号。"""
    spec = read(SPEC)
    lines = spec.splitlines()

    # 表格式条目： | 9.12 | `test_...`
    table_nums: list[int] = []
    for i, line in enumerate(lines, 1):
        m = re.match(r"^\|\s*9\.(\d+)\s*\|", line)
        if m:
            table_nums.append(int(m.group(1)))

    dup = {n for n in table_nums if table_nums.count(n) > 1}
    for n in sorted(dup):
        err(f"SPEC.md 测试条目 9.{n} 在表格中出现 {table_nums.count(n)} 次（编号重复）")

    # 规则小节标题： ### 9.37 不得预设结果
    rule_nums = [int(m.group(1)) for line in lines
                 if (m := re.match(r"^###\s+9\.(\d+)\s", line))]
    for n in rule_nums:
        if n <= 36:
            err(f"SPEC.md 规则小节 §9.{n} 落在测试条目区间（9.1–9.36），编号冲突")

    # 区间表与实际一致
    declared = re.search(r"§9\.1–§9\.(\d+)\s*是测试条目", spec)
    if declared:
        hi = int(declared.group(1))
        actual_hi = max(table_nums) if table_nums else 0
        if hi != actual_hi:
            err(f"SPEC.md 声称测试条目到 9.{hi}，但表格最大编号为 9.{actual_hi}")
    else:
        warn("SPEC.md 未找到『§9.1–§9.x 是测试条目』的声明行")


def check_test_names() -> None:
    """`test_invariants.py` 的函数名与 SPEC 表格中登记的必须一致。

    ⚠️ **只比对 `test_invariants.py`**（SPEC §9 的守门测试）。
    `test_chemistry_params.py` 的参数测试**故意不进 §9 编号**，
    比对它会产生大量误报。
    """
    spec = read(SPEC)
    spec_tests = set(re.findall(r"`(test_[a-z0-9_]+)`", spec))
    py = read(TESTS / "test_invariants.py")
    py_tests = set(re.findall(r"^def (test_[a-z0-9_]+)", py, re.M))

    for t in sorted(spec_tests - py_tests):
        warn(f"SPEC 登记了 {t}，但 test_invariants.py 未定义（可能改名或未创建）")
    for t in sorted(py_tests - spec_tests):
        warn(f"test_invariants.py 定义了 {t}，但 SPEC §9 未登记")


# ---------------------------------------------------------------- 3. 版本一致性

def check_versions() -> None:
    m = re.search(r"SPEC v(\d+\.\d+)", read(SPEC))
    if not m:
        err("SPEC.md 标题行未找到版本号（形如 'SPEC v2.9'）")
        return
    ver = m.group(1)
    # 只要求"当前版本号出现过"；允许同时出现历史版本（如"v2.1 修正"）
    for p in (README, PROMPT):
        if f"v{ver}" not in read(p):
            err(f"{p.name} 未提及当前版本 v{ver}")
    if f"## v{ver}" not in read(CHANGELOG):
        err(f"CHANGELOG.md 缺少当前版本 v{ver} 的小节")


# ---------------------------------------------------------------- 4. 关键常量一致

def check_constants() -> None:
    """同一常量在 SPEC 与配置中必须一致（历史错误高发区）。"""
    spec = read(SPEC)
    econ = read(CONFIGS / "economics.yaml")
    exp = read(CONFIGS / "experiments.yaml")

    checks = [
        ("tau_rel", r"tau_rel:\s*([0-9.eE+-]+)", r"τ_rel\s*=\s*([0-9.eE+-]+)"),
        ("tau_floor", r"tau_floor:\s*([0-9.eE+-]+)", r"τ_floor\s*=\s*([0-9.eE+-]+)"),
        ("epsilon_shed_mw", r"epsilon_shed_mw:\s*([0-9.eE+-]+)",
         r"ε_shed\s*=\s*([0-9.eE+-]+)"),
        ("epsilon_heat_mw_th", r"epsilon_heat_mw_th:\s*([0-9.eE+-]+)",
         r"ε_heat\s*=\s*([0-9.eE+-]+)"),
    ]
    for name, cfg_pat, spec_pat in checks:
        cfg_m = re.search(cfg_pat, exp) or re.search(cfg_pat, econ)
        if not cfg_m:
            err(f"配置中未找到 `{name}`（SPEC 若引用它则实现会读不到）")
            continue
        if not re.search(spec_pat, spec):
            warn(f"SPEC 正文未出现与 `{name}` 对应的符号（ε/τ 记号）")


# ---------------------------------------------------------------- 5. 废弃概念

def check_banned_concepts() -> None:
    """已作废机制**只在说明性语境**出现，不得作为待实现要求。

    ⚠️ 这是启发式检查，**必然有误报**：作废说明本身就会提到这些词。
    判据是"同一行是否含作废标记"（已作废/已删除/不要/历史/旧/v1./v2.1/v2.2）。
    WARN 级别，需人工确认，不作为交付门槛。
    """
    banned = {
        "gamma_eff": "已删除（v1.3）",
        "z_boiler": "已删除（v1.9）",
        "critical_load_fraction": "已删除（v2.3）",
        "heat_tolerance": "已删除",
        "it_base_mw": "已改为导出量（v1.6）",
        "Q_hp_max(t)": "已改为常数差额上界（v2.3）",
    }
    allow_ctx = ("已作废", "已删除", "不要", "历史", "作废", "旧",
                 "v1.", "v2.1", "v2.2", "已改为", "已废弃")
    for p in live_files():
        if p.suffix not in {".py", ".yaml", ".md"} or p.name in ARCHIVE_OK:
            continue
        for i, line in enumerate(read(p).splitlines(), 1):
            for tok, why in banned.items():
                if tok in line and not any(a in line for a in allow_ctx):
                    warn(f"{p.relative_to(ROOT)}:{i} 出现废弃概念 {tok}"
                         f"（{why}）但无作废说明：{line.strip()[:70]}")


# ---------------------------------------------------------------- 6. 分级表一致

def check_solver_markers() -> None:
    """§9.A / §9.B 的分级必须与代码中的 @pytest.mark.solver 一致。"""
    spec = read(SPEC)
    py = read(TESTS / "test_invariants.py")
    marked = set(re.findall(
        r"@pytest\.mark\.solver\s*(?:\n@pytest\.mark\.\w+\s*)*\ndef (test_\w+)", py))

    # 非求解器组：声明"不需要求解器"的表内测试
    sec_a = spec.split("### 9.A")[1].split("### 9.B")[0] if "### 9.A" in spec else ""
    num_to_name = dict(re.findall(r"^\|\s*9\.(\d+)\s*\|\s*`(test_\w+)`", spec, re.M))
    listed_a = set(re.findall(r"`(test_\w+)`", sec_a))
    for t in sorted(listed_a & marked):
        warn(f"{t} 被 §9.A 声明为不需要求解器，但代码带了 @pytest.mark.solver")

    sec_b = spec.split("### 9.B")[1].split("### 测试组 A")[0] if "### 9.B" in spec else ""
    for num in re.findall(r"9\.(\d+)", sec_b):
        name = num_to_name.get(num)
        if name and name not in marked:
            warn(f"{name}（9.{num}）被 §9.B 列为需要求解器，但代码未带 @pytest.mark.solver")


# ---------------------------------------------------------------- 7. 轨迹列

def check_trajectory_columns() -> None:
    """SPEC §8.2 的轨迹列与源码 TRAJECTORY_COLUMNS 必须双向一致。

    只解析 §8.2 代码块中**形如逗号分隔列名**的行，忽略散文行，
    避免把说明文字误当列名。
    """
    spec = read(SPEC)
    m = re.search(r"### 8\.2 轨迹 DataFrame 的必须列名\s*```\n(.*?)```", spec, re.S)
    if not m:
        err("未找到 §8.2 的轨迹列代码块")
        return

    spec_cols: set[str] = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or not re.match(r"^[A-Za-z_][A-Za-z0-9_,\s]*$", line):
            continue                      # 含中文或非列名格式 → 散文，跳过
        parts = [t.strip() for t in line.split(",")]
        spec_cols |= {t for t in parts if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t)}

    py = read(SRC / "aidc" / "model" / "dispatch.py")
    m2 = re.search(r"TRAJECTORY_COLUMNS\s*=\s*\[(.*?)\n\]", py, re.S)
    if not m2:
        err("dispatch.py 未找到 TRAJECTORY_COLUMNS 的闭合定义")
        return
    code_cols = set(re.findall(r'"([^"]+)"', m2.group(1)))

    missing = code_cols - spec_cols
    if missing:
        err(f"轨迹列在源码中存在但 SPEC §8.2 未列出：{sorted(missing)}")
    extra = spec_cols - code_cols
    if extra:
        err(f"SPEC §8.2 列出了源码中没有的列：{sorted(extra)}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有 ERROR 时返回码 1")
    args = ap.parse_args()

    if not SPEC.exists():
        print(f"找不到 {SPEC}", file=sys.stderr)
        return 2

    check_dangling_refs()
    check_section_refs()
    check_test_numbering()
    check_test_names()
    check_versions()
    check_constants()
    check_banned_concepts()
    check_solver_markers()
    check_trajectory_columns()

    print(f"selfcheck: {len(errors)} ERROR, {len(warns)} WARN\n")
    for e in errors:
        print(f"  [ERROR] {e}")
    if errors:
        print()
    for w in warns:
        print(f"  [WARN ] {w}")

    if not errors and not warns:
        print("  全部检查通过。")
    print("\n注意：本工具只检查一致性（引用、编号、常量、废弃概念），"
          "\n      不检查物理与数学正确性——那仍需人工复核。")

    return 1 if (args.strict and errors) else 0


if __name__ == "__main__":
    sys.exit(main())
