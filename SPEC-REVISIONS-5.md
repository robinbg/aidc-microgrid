> ## ⚠️ 本文件已作废 · DEPRECATED — 请勿据此实现
>
> **唯一权威规格是 [`SPEC.md`](./SPEC.md)（版本号见其标题行）。** 本文件是 v1.5 时期的历史留档，
> 其全部**有效**结论已并入 SPEC.md，其余内容**已被后续版本推翻**。
>
> ⚠️ **下方"权威性"那句话已失效**——那是 v1.5 当时的约定，
> 现在**恰好相反**：冲突时一律以 `SPEC.md` 为准。
>
> 本文件可能含**已作废的错误结论**，照它实现会写出错的代码。
> 正确口径以 `SPEC.md` 为准，变更缘由见 [`CHANGELOG.md`](./CHANGELOG.md)。

# SPEC 决策附录 · 第五批（v1.5）

本文件回应代码代理 M1 阶段提出的 2 处契约缺口（下称 Y1–Y2）。

**权威性**：`SPEC.md` < `SPEC-REVISIONS.md` < `SPEC-REVISIONS-2.md`
< `SPEC-REVISIONS-3.md` < `SPEC-REVISIONS-4.md` < **本文件**。

**修订日期**：2026-10-12

**范围**：补齐数据生成配置（Y1）与修正柔性占比的计量口径（Y2）。
γ 机制、能量上限、条件化不变量、W2、W3、X3/X4 **均不变**。

---

## 总览

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| Y1 | 生成器所需参数缺失（Weibull、功率曲线、任务分布等） | 配置缺口 | Z1 补齐 `configs/generation.yaml` |
| Y2 | 柔性占比缺少可核验的全系统分母，按任务表算必然 100% | **口径错误** | Z2 改为电量口径 + 由目标比反解任务规模 |

---

## Z1 · 数据生成参数（应 Y1）

### 已补齐

新建 **`configs/generation.yaml`**，包含：

- **风电**：Weibull 形状参数 k 与尺度参数 c（各含来源说明）、AR(1) 自相关系数、
  季节与日周期调制幅度、**风机功率曲线**（分段线性，含切入/额定/切出风速）
- **光伏**：三类天气的晴空指数分布、天气状态马尔可夫转移矩阵、组件温度系数
- **环境温度**：日周期最低/最高温出现时刻
- **任务生成**：GPU 需求离散取值与权重、时长分布、S2/S3 的截止时间松弛分布、
  任务到达强度的日周期剖面
- **生成约束**：窗口越界的自校验规则、重采样策略与失败处理

### 授权说明（回应 PROMPT.md 的禁硬编码要求）

`configs/generation.yaml` **由规格方提供，不是让你自行拟定**。其中的数值分两类：

1. **物理/统计形式参数**（Weibull k、c；功率曲线；温度系数等）：
   标注为 `placeholder`，需用公开数据标定。当前取值是行业典型范围，
   足以产生统计特征合理的序列。
2. **仿真设定值**（GPU 取值集合、时长分布、松弛区间等）：
   标注为 `仿真设定值`，无公开数据可标定，属方案设定。

**报告口径要求**：凡引用生成序列的结果，必须标注为"基于仿真生成的气象与负载序列"，
并按 `generation.yaml` 的 source 字段说明哪些参数已标定、哪些仍为设定值。
不得表述为真实运行数据（SPEC §10.1）。

### 关于"容量因子不能唯一确定 Weibull 参数"

Y1 的判断正确。容量因子是 Weibull 参数与功率曲线共同作用的**结果**，
反过来不能唯一确定它们。因此 `generation.yaml` **直接给出 k、c 与功率曲线**，
而 `regions/*.yaml` 中的 `cf_pv` / `cf_wind` 改为**校验用参考值**：

- 生成器按 `generation.yaml` 的参数生成逐时出力
- 然后计算实际的容量因子，与 `regions/*.yaml` 的 `cf_wind` / `cf_pv` 比较
- **偏差超过 ±0.05 时告警**，但不自动调整参数——调整依据应由标定决定

这样容量因子从"输入"变成"可核验输出"，口径更清晰。

---

## Z2 · 柔性占比的计量口径（应 Y2）

### 问题确认

Y2 正确，且这是**口径错误而非配置缺口**。原定义：

```
flexible_ratio = ( S2 + S3 算力需求 ) / ( 总算力需求 )
```

而 U8 已明确 S1 不进任务表。因此"仅对返回表计算"时分子与分母都只剩 S2+S3，
**必然得到 1.0（100%）**，与 R-NW 设定的 0.65 无法比较。原设计的"核验"是空转。

### 修正：分母取全系统 IT 用电量

**统一用电量口径**（MWh），不用 GPU·h——因为 IT_base 是电功率序列，
与任务负荷可以直接相加，且与后续的 LCOE 分母（年服务电量，U11）口径一致。

```
E_S1    = Σ_t IT_base(t) · Δt                        [MWh]   不可迁移负荷（含 S1）
E_tasks = Σ_i gpu_i · p_card · dur_i                 [MWh]   规划任务负荷（S2+S3）
E_IT    = E_S1 + E_tasks                             [MWh]   全系统 IT 用电量

flexible_ratio = E_tasks / E_IT
```

其中 `p_card` 为单卡功率（MW/卡，由 `system.yaml` 的 `gpu_power` 换算，见 §U1）。

**这是唯一口径。** 报告中同时给出三项电量的绝对值，便于复核。

### 生成器如何命中目标比例

由目标比例 `R` 与已知的 `E_S1` 反解所需的任务电量：

```
E_tasks_target = E_S1 · R / ( 1 − R )
```

流程（`generate_tasks` 实现）：

1. 计算 `E_S1 = Σ_t IT_base(t)·Δt`（由时间序列直接得出，不依赖任务生成）
2. 由 `R` 反解 `E_tasks_target`
3. 按 `generation.yaml` 的分布采样任务，累加电量，逐步逼近
   `E_tasks_target`（采样到总电量达到目标即可停止，任务数不必预先固定）
4. 重新计算实际 `flexible_ratio`，与目标比较

**⚠️ 禁止反向操作**：不得先固定任务数、算出实际比例、再宣称"核验通过"。
必须由目标比例驱动生成。

### 接口修正

```python
def generate_tasks(
    it_base_mwh: float,          # 窗口内不可迁移 IT 电量（E_S1），由时间序列算出
    horizon_hours: int,
    gpu_capacity_plan: int,      # 规划可用卡数（= total − S1 reserved）
    flexible_ratio: float,       # 目标柔性占比 R
    seed: int,
    p_card_mw: float,            # 单卡功率（MW/卡）
    s3_tolerance_hours: float = 12.0,
    gen_cfg: dict | None = None, # configs/generation.yaml 的内容
) -> pd.DataFrame:
    """生成算力任务表（SPEC-REVISIONS-5 §Z2）。

    ⚠️ `it_base_mwh` 是必需参数，不是可选项（v1.5 新增）。
    缺少它就无法计算全系统分母，柔性占比无法核验——这正是 Y2 指出的问题。

    返回的 DataFrame 除 TASK_COLUMNS 外，需在 attrs 中附带：

        attrs["E_S1_mwh"]           = it_base_mwh
        attrs["E_tasks_mwh"]        = 实际任务电量
        attrs["flexible_ratio"]     = E_tasks / (E_S1 + E_tasks)
        attrs["flexible_ratio_target"] = 输入的目标 R
        attrs["n_resample"]         = 因窗口越界而重采样的次数

    这样下游与测试可直接取用，无需重算。
    """


def summarize_tasks(
    tasks: pd.DataFrame,
    horizon_hours: int,
    it_base_mwh: float,          # v1.5 新增，必需
    p_card_mw: float,            # v1.5 新增，必需
) -> dict:
    """任务表统计摘要。

    返回值必须包含：

        E_S1_mwh, E_tasks_mwh, E_IT_mwh          三项电量绝对值
        flexible_ratio                            实际柔性占比
        flexible_ratio_target                     目标值
        flexible_ratio_deviation                  实际 − 目标（用于核验）
        各级任务数量、GPU 总需求、最坏情形 SLA 惩罚量

    ⚠️ 校验：若 |flexible_ratio_deviation| > 0.05，需告警并说明原因
    （任务电量难以精确命中目标时，应在报告中给出实际值，而非调整目标）。
    """
```

### 校验规则（替换原 U17 的柔性比校验）

原 U17 要求"比较生成结果的柔性占比与画像设定"，但未定义分母，无法执行。现在：

```
def test_flexible_ratio_matches_target():
    ts = build_timeseries(region="R-NW", fidelity="low", ...)
    E_S1 = float((ts["IT_base"] * 1.0).sum())          # Δt = 1 h
    tasks = generate_tasks(it_base_mwh=E_S1, ..., flexible_ratio=0.65, ...)
    R_actual = tasks.attrs["flexible_ratio"]

    assert abs(R_actual - 0.65) < 0.05, (
        f"柔性占比 {R_actual:.3f} 偏离目标 0.65 超过容差；"
        "应检查生成器的逼近逻辑，而不是调整目标值"
    )
    # 并断言分母确实含 S1
    assert tasks.attrs["E_IT_mwh"] > tasks.attrs["E_tasks_mwh"]
```

**容差 ±0.05 的依据**：任务电量是离散采样的总和，任务数较少时（168 h 窗口约 120 项）
存在采样波动。若偏差持续超过容差，说明逼近逻辑有 bug，**不得通过修改目标值来"通过"测试**。

### 代表周下的口径一致性

低保真使用 5 个 168 h 代表周（W3）。柔性占比应在**每个代表周内分别计算并核验**，
而不是跨周合并——不同季节的 IT_base 与任务剖面不同。

报告中给出五个周的柔性占比及其目标值，若某个周显著偏离，需说明原因
（例如该周的 IT_base 剖面与任务到达剖面不匹配）。

---

## 附：需要同步修改的文件

| 文件 | 修改点 |
|---|---|
| `configs/generation.yaml` | **新建**（Y1） |
| `configs/regions/*.yaml` | `cf_pv` / `cf_wind` 的注释改为"校验用参考值"（Z1） |
| `src/aidc/data/tasks.py` | `generate_tasks` / `summarize_tasks` 增加 `it_base_mwh`、`p_card_mw` 参数；口径改为电量；`attrs` 回传三项电量（Z2） |
| `src/aidc/data/timeseries.py` | docstring 补 `generation.yaml` 的参数来源；容量因子改为输出后校验（Z1） |
| `tests/test_invariants.py` | 新增 `test_flexible_ratio_matches_target`（Z2） |
| `PROMPT.md` | 阶段二的 M1 范围说明中，`configs/generation.yaml` 列为"已提供，勿改" |

---

## 给代码代理的确认

1. **Y1、Y2 均成立**。Y1 是配置缺口——应由规格方提供参数，不该让你猜；
   已补齐 `configs/generation.yaml`，**你无需自行拟定，也不要修改该文件**。
2. Y2 是**口径错误**，你指出的"仅对返回表计算必然得到 100%"完全正确。
   已改为电量口径，分母含 S1，并由目标比例反解任务规模。
3. 你关于"不能由目标比例反推分母后再宣称核验通过"的表述，我按此写成了硬约束：
   **必须由目标驱动生成，而不是先算再说**。这条已写进 Z2 的流程说明与测试断言。
4. 两项均不影响 γ 机制、能量上限与已确认的不变量。
5. M1 守门测试更新为：
   - `test_all_configs_parse`（含新增的 `generation.yaml`）
   - `test_curve_blocks_have_points_key`、`test_curve_scale_range_present`
   - `test_unit_conversion_factor_applied`
   - `test_usable_band_scales_with_gamma`
   - `test_energy_limits_positive_part`、`test_bus_side_limits_respect_efficiency`
   - **`test_flexible_ratio_matches_target`（新增，Z2）**
   - **`test_capacity_factor_matches_region_reference`（新增，Z1）**
   - `test_representative_week_tasks_within_window`、`test_representative_week_weights_sum_to_one`
6. 若在实现中再发现缺口，仍按原约定：**停下来报告，不要自行补 YAML 或选择口径**。
