# SPEC 决策附录 · 第六批（v1.6）

本文件回应代码代理 M1 阶段提出的 3 处契约缺口（下称 AB1–AB3）。

**权威性**：`SPEC.md` < `SPEC-REVISIONS.md` < `SPEC-REVISIONS-2.md`
< `SPEC-REVISIONS-3.md` < `SPEC-REVISIONS-4.md` < `SPEC-REVISIONS-5.md` < **本文件**。

**修订日期**：2026-10-12

**范围**：修正电量口径下的容量自洽性（AB1）、补齐 S2/S3 权重（AB2）、
补齐极端代表周的年末边界规则（AB3）。
γ 机制、能量上限、条件化不变量、W2、W3、X3/X4、Z1/Z2 **均不变**，
但 AB1 会改变 `system.yaml` 与四份 region 配置中的规模参数。

---

## 总览

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| AB1 | 目标柔性占比超过 GPU 容量上界，四画像全部不可达 | **容量冲突（根因是配置不自洽）** | AA 改 IT_base 为导出量 + 重定规模 + 加预检 |
| AB2 | S2/S3 采样概率未配置 | 配置缺口 | AB 写入 `priority_mix` |
| AB3 | 极端代表周缺少年末边界规则 | 配置缺口 | AC 改为多生成 168 h |

---

## AA · 容量自洽性与规模重定（应 AB1）

### 根因确认

AB1 的计算全部正确。但根因比"目标占比设得太高"更基础：

```
v1.5 配置：gpu_capacity_s1_reserved = 1024 卡，it_base_mw = 8.0 MW
不自洽：    1024 × 0.7 kW = 0.7168 MW ≠ 8.0 MW        相差约 11 倍
```

`it_base_mw` 被当成独立旋钮设定，与它名义上占用的卡数脱钩。由此：

- 分母 `E_S1` 被抬高一个数量级
- 分子上界 `E_tasks_max` 受规划卡数限制，保持不变
- 结果：需求侧电量远超供给侧，四个画像的目标占比全部不可达

**这不只是数值调参问题**，说明 `IT_base` 与 GPU 规模之间缺少硬约束。
因此修正的第一条是**让两者建立导出关系**，使此类冲突在配置层不可能发生。

### 硬约束（新增）

```
IT_base(t) = gpu_capacity_s1_reserved × gpu_power + it_overhead_mw

其中 gpu_capacity_s1_reserved = round( gpu_capacity_total × s1_reserved_fraction )
     gpu_capacity_plan       = gpu_capacity_total − gpu_capacity_s1_reserved
```

- **`IT_base` 是导出量**：配置中不再出现 `it_base_mw` 这一项（保留在注释中说明推导）
- `s1_reserved_fraction` 用**比例**而非绝对卡数：比例随总规模缩放，不会与总量脱钩
- `it_overhead_mw` 用于与算力无关的固定负荷（照明、办公、消防），默认 0

**为什么用比例**：v1.5 同时给出 `gpu_capacity_total` 与 `gpu_capacity_s1_reserved` 两个
绝对值，二者可以互相矛盾；改为比例后，只要 `fraction ∈ (0, 1)`，关系自动自洽。

### 规模重定

为满足目标占比且留出可调度余量，总规模上调：

```
gpu_capacity_total = 6144 卡
gpu_power          = 0.7 kW/卡 = 0.0007 MW/卡
总装机             = 6144 × 0.0007 = 4.3008 MW
```

四画像的推导与可达性校验：

| 画像 | 目标 R | `s1_reserved_fraction` | 预留卡数 | 规划卡数 | `IT_base` (MW) | `E_S1` (MWh) | `E_tasks` 需求 (MWh) | 可发电量上界 (MWh) | 所需平均利用率 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| R-NW | 0.65 | 0.167 | 1026 | 5118 | 0.7182 | 120.66 | 224.08 | 601.88 | **0.372** |
| R-WPV | 0.70 | 0.167 | 1026 | 5118 | 0.7182 | 120.66 | 281.54 | 601.88 | **0.468** |
| R-EAST | 0.45 | 0.25 | 1536 | 4608 | 1.0752 | 180.63 | 147.79 | 541.90 | **0.273** |
| R-SOUTH | 0.50 | 0.25 | 1536 | 4608 | 1.0752 | 180.63 | 180.63 | 541.90 | **0.333** |

计算口径（窗口 168 h）：

```
E_S1         = IT_base × 168
E_tasks 需求 = E_S1 · R / (1 − R)
可发电量上界 = 规划卡数 × 0.0007 × 168          （连续满载）
所需平均利用率 = E_tasks 需求 / 可发电量上界
```

**四画像的所需利用率均 ≤ 0.47**，为到达、截止与非抢占约束留出充足余量。
`R-WPV` 的 0.468 最高，因为它的目标占比也最高（0.70）。

**这是"宽松上界"口径**：所需利用率是按连续满载算的，实际调度受峰值与截止约束，
真值会更高。**通过此校验不等于任务集一定可调度**——可调度性的真正验证在 M2 求解阶段。

### 新增预检（必须实现）

```python
def check_flexible_ratio_feasible(
    it_base_mw: float,
    horizon_hours: int,
    gpu_capacity_plan: int,
    p_card_mw: float,
    target_ratio: float,
    max_required_avg_utilization: float = 0.60,
) -> dict:
    """柔性占比可达性预检（SPEC-REVISIONS-6 §AA）。

        E_S1        = it_base_mw · horizon_hours
        E_tasks_req = E_S1 · R / (1 − R)
        E_tasks_max = gpu_capacity_plan · p_card_mw · horizon_hours
        u_req       = E_tasks_req / E_tasks_max

    若 u_req > max_required_avg_utilization，**报错并说明**，
    不得静默降低目标占比、不得调整 it_base、不得放宽 GPU 容量。

    错误信息须包含：目标 R、E_tasks 需求、可发电量上界、所需利用率、阈值。

    Returns:
        {"E_S1_mwh", "E_tasks_req_mwh", "E_tasks_max_mwh",
         "required_avg_utilization", "threshold", "feasible": bool}
    """
```

**阈值 0.60 的依据**：留 40% 余量覆盖峰值、到达分布与截止约束带来的不可用时段。
该值写入 `generation.yaml` 的 `task_generation_constraints.max_required_avg_utilization`。

### 参数归属调整

| 参数 | 原位置 | 新位置 | 说明 |
|---|---|---|---|
| `it_base_mw` | `system.yaml`、各 region | **删除**（改为导出） | 冲突根源 |
| `gpu_capacity_s1_reserved` | `system.yaml`（绝对值） | **改为** `s1_reserved_fraction`（比例） | 防止与总量脱钩 |
| `gpu_capacity_plan` | 各 region（绝对值） | **改为导出** | 同上 |
| `gpu_capacity_total` | `system.yaml` + region 覆盖 | 保留，region 可覆盖 | 6144 |
| `s1_reserved_fraction` | — | `system.yaml` 默认 0.167，region 可覆盖 | R-EAST / R-SOUTH 为 0.25 |
| `it_overhead_mw` | — | `system.yaml`，默认 0 | 与算力无关的负荷 |

### 报告口径要求

**必须报告**：总装机（MW）、预留与规划卡数、`IT_base`、`E_S1`、`E_tasks` 需求、
可发电量上界、所需平均利用率，以及**通过预检不等于可调度**的声明。

**不允许**：为让占比达标而隐含放大任务电量、或为让调度可行而缩小目标占比。
两者都会使"柔性占比"这一维度失去意义。

---

## AB · S2/S3 的采样权重（应 AB2）

`configs/generation.yaml` 已补 `tasks.priority_mix`：

```yaml
tasks:
  priority_mix:
    S2: 0.4667    # = 7/15
    S3: 0.5333    # = 8/15
```

**取值依据**：由原 `priority_mix = {S1: 0.25, S2: 0.35, S3: 0.40}` 中
S2 与 S3 的相对比例归一化而来（0.35 : 0.40 = 7 : 8）。
S1 按 U8/U10 不进任务表，故不参与。

**加载时校验**：两项之和须为 1.0（容差 1e-6），否则报错。

同时补了 `tasks.by_priority`，允许 S2 与 S3 使用**不同的** GPU/时长/松弛分布——
此前只有一套全局分布，隐含假设两类任务负载特征相同，这不合理：
S2（微调）中等 GPU、较短时长、截止较紧；S3（批推理/训练）较大 GPU、较长时长、截止较宽。

---

## AC · 极端代表周的年末边界（应 AB3）

### 问题确认

AB3 正确。`W-WINX` 定义为"从全年最低温时刻起向后取连续 168 h"，
若最低温落在最后 167 h 内，窗口越界。输入只有 8760 h，无法取得完整窗口。

### 选定方案：多生成 168 小时

```
生成序列总长度 = 8760 + 168 = 8928 h
```

**为什么选这个方案**（对比三个选项）：

| 方案 | 语义影响 | 评价 |
|---|---|---|
| 窗口提前 | 取到的是"最冷周之前的 168 h"，**不再是最冷周** | 语义被改变，不用 |
| 补生成下一年 | 等价于本方案，但需跨年拼接、伪影风险（年际不连续） | 本方案更简洁 |
| **周期拼接** | 取到的是"年末 167 h + 年初 1 h"，**跨年边界不真实** | 语义被改变，不用 |
| **多生成 168 h** | 全年段内真实取窗，**语义不变** | **采用** |

### 使用规则（必须遵守）

```
年度统计（容量因子校验、E_S1、年电量、年度指标）→ **只用前 8760 小时**
代表周选取（W-WIN / W-WINX / W-SPR / W-SUM / W-AUT）→ 可在全段 8928 小时内取窗
极端周的定义                                        → 前 8760 h 内温度最低点起，向后 168 h
```

**⚠️ 关键**：`W-WINX` 的**最低温点**须在前 8760 h 内搜索，
但**窗口可以延伸到 8928 h**。这样"全年最冷连续 168 小时"的语义
与"年度统计只用一年"的口径同时成立。

**年度统计不得包含多生成的 168 h**——否则容量因子、年电量等会系统性偏高约 1.9%。

### 配置

```yaml
series:
  year_hours: 8760
  extra_hours: 168
  total_hours: 8928
  note: "年度统计只用前 8760 小时；代表周可在全段内选取"
```

### 新增测试

```python
def test_extended_series_length():
    """生成序列长度为 8928 h，且年度统计确实只用前 8760 h。"""

def test_extreme_week_window_within_bounds():
    """W-WINX 的窗口必须完整落在 8928 h 内，
    且起点为前 8760 h 内的最低温时刻。构造一个最低温落在
    第 8700 小时的合成序列，验证窗口不越界。"""
```

---

## 附：需要同步修改的文件

| 文件 | 修改点 |
|---|---|
| `configs/generation.yaml` | 补 `priority_mix`、`by_priority`、`series`、可达性预检阈值（AB、AC） |
| `configs/system.yaml` | 删 `it_base_mw`、`gpu_capacity_s1_reserved`、`gpu_capacity_plan`；加 `s1_reserved_fraction`、`it_overhead_mw`；总卡数 6144（AA） |
| `configs/regions/*.yaml` | 删 `it_base_mw`、`gpu_capacity`；加 `s1_reserved_fraction`；注释明确 IT_base 为导出量（AA） |
| `src/aidc/data/tasks.py` | 新增 `check_flexible_ratio_feasible`；`generate_tasks` 调用该预检（AA） |
| `src/aidc/data/timeseries.py` | 序列长度改为 8928；年度统计限定前 8760 h；`select_representative_weeks` 按 AC 取窗（AC） |
| `tests/test_invariants.py` | 新增 `test_flexible_ratio_feasible_pretest`、`test_extended_series_length`、`test_extreme_week_window_within_bounds` |

---

## 给代码代理的确认

1. **AB1、AB2、AB3 全部成立**。AB1 你算得很准，而且你的表格帮我把根因定位到了
   `it_base_mw` 与卡数不自洽，而不是表面上的"目标占比太高"。
2. AB1 的第一条修法是**结构性的**：`IT_base` 改为导出量、`s1_reserved` 改为比例。
   这样"分母与供给脱钩"这类错误在配置层就无法构造。
3. 你要求"增加总任务电量上界预检，但通过该预检仍不代表所有时间窗口可调度"——
   已按此实现，并把该声明写进了预检的文档与报告口径要求。
4. AB2 采用归一化后的 7/15、8/15，**已写入配置**，你无需自行补。
   另外补了按优先级分组的分布，因为 S2 与 S3 的负载特征不同。
5. AB3 采用"多生成 168 h"，并在文档里写明了它与"窗口提前""周期拼接"的语义差异，
   以及**年度统计只用前 8760 h** 的硬要求。
6. M1 守门测试更新为（共 14 条）：
   - `test_all_configs_parse`（含新字段）
   - `test_curve_blocks_have_points_key`、`test_curve_scale_range_present`
   - `test_unit_conversion_factor_applied`
   - `test_usable_band_scales_with_gamma`
   - `test_energy_limits_positive_part`、`test_bus_side_limits_respect_efficiency`
   - `test_flexible_ratio_matches_target`
   - **`test_flexible_ratio_feasible_pretest`（新增，AA）**
   - `test_capacity_factor_matches_region_reference`
   - `test_representative_week_tasks_within_window`、`test_representative_week_weights_sum_to_one`
   - **`test_extended_series_length`、`test_extreme_week_window_within_bounds`（新增，AC）**
7. 若在实现中再发现缺口，仍按原约定：**停下来报告，不要自行补 YAML 或选择口径**。
