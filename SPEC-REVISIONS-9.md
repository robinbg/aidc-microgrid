> ## ⚠️ 本文件已作废 · DEPRECATED — 请勿据此实现
>
> **唯一权威规格是 [`SPEC.md`](./SPEC.md)（版本号见其标题行）。** 本文件是 v1.9 时期的历史留档，
> 其全部**有效**结论已并入 SPEC.md，其余内容**已被后续版本推翻**。
>
> ⚠️ **下方"权威性"那句话已失效**——那是 v1.9 当时的约定，
> 现在**恰好相反**：冲突时一律以 `SPEC.md` 为准。
>
> 本文件可能含**已作废的错误结论**（如 `critical_load_fraction` 三分数结构，
> 已于 v2.3 删除），照它实现会写出错的代码。
> 正确口径以 `SPEC.md` §5.2 为准，变更缘由见 [`CHANGELOG.md`](./CHANGELOG.md)。

# SPEC 决策附录 · 第九批（v1.9）

本文件回应代码代理第四轮 M1 阻断（下称 AH1–AH4）。

**权威性**：`SPEC.md` < `SPEC-REVISIONS.md` < … < `SPEC-REVISIONS-8.md` < **本文件**。

**修订日期**：2026-10-12

**范围**：修正登记结构（AH1）、A/B/C 算例数值（AH2）、需求侧上限的冗余论断（AH3）、
v1.8 的不可行论证（AH4）。**min/max 分配规则本身不变**（已确认可采用）。

---

## 总览

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| AH1 | registry 中两个 `quantity_id` 指向同一位置，必然违反自身断言 | 登记结构错误 | AI1 只登记配置真源，派生量另立类型 |
| AH2 | A/B/C 算例混入旧 B 的数值 | 验算错误 | AI2 重算，并重新表述锅炉成本项的作用 |
| AH3 | 需求侧上限仍独立，不是冗余 | 推理错误 | AI3 保留上限，删除"自动满足"说明 |
| AH4 | 初稿四式的"整体不可行"论证不成立 | 论证过强 | AI4 改为"仅 z=1 分支不可行"，并给出 min/max 更优的正确理由 |

**AH1–AH4 全部成立。**

---

## AI1 · 登记结构（应 AH1）

### 问题

`registry.yaml` 把两个 `quantity_id` 登记到同一位置：

```yaml
  - quantity_id: gpu_single_card_power
    authorized: "system.compute.gpu_power"
  - quantity_id: single_card_power_mw
    authorized: "system.compute.gpu_power"      # ← 与上一条同位置
```

而 `test_canonical_quantities_unique` 断言"没有两个 `quantity_id` 指向同一位置"。
**登记本身必然让该测试失败。**

### 修正：区分「配置真源」与「派生量」

登记表分为两类条目，各自有不同约束：

```yaml
# 配置真源：必须存在，且位置唯一
canonical_config_truths:
  - quantity_id: gpu_single_card_power
    authorized: "system.compute.gpu_power"
    forbidden_aliases: ["system.compute.gpu_power_kw_per_card", "system.compute.p_card"]
    history: "v1.1 曾用 gpu_power_kw_per_card（含单位后缀），已改为 gpu_power + unit 字段"

  - quantity_id: boiler_carbon_intensity
    authorized: "econ.heat_carbon.ci_boiler"
    forbidden_aliases: ["system.thermal.backup_heat.ci_kgco2_per_mwh_th"]
  # …（其余同前）

# 派生量：不独立配置，由真源换算而来。**不得作为 authorized 项登记。**
derived_quantities:
  - quantity_id: single_card_power_mw
    derived_from: "system.compute.gpu_power"
    derivation: "value_MW_per_card = value_kW_per_card / 1000"
    must_not_appear_as_config: true
    forbidden_aliases: ["system.compute.p_card_mw"]
    note: "内部单位换算结果，见 §U1 换算表。禁止作为配置项出现。"
```

**判定规则（修正后的 `test_canonical_quantities_unique`）**：

```
1. canonical_config_truths 中，位置两两不同                 否则报错
2. derived_quantities 的 derived_from 必须指向某个配置真源    否则报错
3. derived_quantities 的 quantity_id 不得出现在
   canonical_config_truths 的 authorized 中                  否则报错
4. 所有 forbidden_aliases（两类合计）均不得被定义            否则报错
5. 每个 authorized / derived_from 指向的位置必须存在         否则告警（登记过期）
```

**唯一位置的断言只适用于第 1 类。** 派生量共享真源是正常的——
它们本就是同一个物理量的不同单位表达。

---

## AI2 · A/B/C 算例的重算（应 AH2）

### 参数

1 h，`COP = 4`，电网碳因子 `450 kgCO₂/MWh`，工业与锅炉碳因子 `220 kgCO₂/MWh`，
工业售价 `80 元/MWh_th`，锅炉成本 `400 元/MWh_th`，市政需求 `0.8 MW_th`。

### 重算结果

| 情形 | `Q_hp` | `Q_mun_hp` | `Q_ind` | `Q_boiler` | 购电碳排 | 工业抵扣 | **净碳排** | 旧 S-heat | 新 S-heat |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 1.0 | 0.8 | 0.2 | 0.0 | 0.25×450 = 112.5 | 44.0 | **68.5** | **+16** | **+16** |
| B | 0.8 | 0.8 | 0.0 | 0.0 | 0.2×450 = 90.0 | 0.0 | **90.0** | 0 | 0 |
| C | 0.0 | 0.0 | 0.0 | 0.8 | 0（无热泵） | 0.0 | **176.0** | **0** | **−320** |

### 我此前的两处错误

1. **C 的旧 S-heat 是 0，不是 +80。** `+80` 来自旧表中工业供热为 1.0 的旧 B
   （`Q_ind = 1.0` → `1.0 × 80 = 80`）。新 C 的工业供热为 0，收入为 0，
   旧目标又无锅炉成本项，故净收益恰为 0。
2. **A 与 B 的碳排不相等**（68.5 vs 90.0）。此前写"A 与 B 碳排相同"是错的。

### `Q_unused` 的重算

B 情形 `Q_hp = 0.8` → `P_hp = 0.8/4 = 0.2` → `Q_low_used = 0.2 × (4−1) = 0.6`：

```
Q_unused = Q_rec − 0.6          （此前误写为 Q_rec − 0.8/3）
```

`0.8/3` 是"把产热 0.8 当成 q_low 直接除以 3"的算法，混淆了母线侧产热与热源侧吸热。

### 锅炉成本项到底在修什么（重新表述）

**修正前的实际情况**：A = +16，B = 0，C = 0。**A 已经是三者最优。**

因此我此前"修正前 C 反而最优"的说法是错的。**真正的问题在别处**——
旧目标在"工业无需求"时对余热利用**完全中性**：

设工业接纳为 0、需求 0.8、`P_hp ∈ [0, 0.2]`（受需求侧上限约束）：

| 选项 | `P_hp` | `Q_hp` | `Q_boiler` | 旧 S-heat | 新 S-heat |
|---|---:|---:|---:|---:|---:|
| E（热泵满出力） | 0.2 | 0.8 | 0.0 | **0** | **0** |
| F（只用锅炉） | 0.0 | 0.0 | 0.8 | **0** | **−320** |

**旧目标下 E 与 F 完全并列**，求解器无理由偏好余热。后果：

- 报告的"余热贡献占比"（`Σ Q_mun_hp / Σ Q_mun_demand`）**成为任意值**，
  取决于求解器的并列解选择
- 而该占比正是方案对供热侧的核心指标之一（§AE 的标定目标 50%–120%）

**所以锅炉成本项的作用不是"纠正错误选择"，而是"消除目标中性"。**
修正后 E = 0 严格优于 F = −320，余热利用成为明确最优。这一点必须在报告中说明——
否则会误以为"不改也不会错"。

---

## AI3 · 需求侧上限（应 AH3）

### 问题确认

v1.8 §AH 称需求侧上限"在规则 1 下自动满足，成为冗余约束"。**该论断错误**——
它忽略了 `Q_dump`。

反例（AH3 给出）：需求 `0.8`、工业接纳 `0`、`Q_hp = 1`。

- 分配规则允许：`Q_mun_hp = 0.8`、`Q_ind = 0`、`Q_dump = 0.2` ✓
- 但原需求侧上限为 `P_hp ≤ [0.8×1.05 + 0]/4 = 0.21`，即 `Q_hp ≤ 0.84`
- **`Q_hp = 1` 违反该上限**

分配规则允许的解被需求侧上限排除，说明上限是**独立的、有约束力的**，不是冗余。

### 修正：保留上限，并明确其作用

**需求侧上限保留，作为独立硬约束**：

```
P_hp(t) ≤ [ Q_mun_demand(t) + Q_ind_capacity(t) ] / COP
```

**它的作用（新的表述）**：把热泵出力限制在"可被有效吸收"的范围内。
没有它，模型对"不吸余热"（`Q_unused > 0`）与"吸上来再弃热"（`Q_dump > 0`）
**完全中性**——两者成本相同、收益相同，构成退化（多个等价最优解）。

加上限后，`P_hp` 不可能超过可吸收量，因此在正常模式下：

```
Q_dump(t) ≡ 0                    （可测的不变量）
```

`Q_dump` 因此退化为**故障模式与诊断用的变量**，不再是正常调度的自由度。

### ⚠️ 顺带修正：删除 `heat_tolerance`

`heat_tolerance` 是 v1.1（§U12）为"避免求解器在刚性需求上出现数值困难"引入的。
**现在有两个理由删除它**：

1. **min/max 规则已处理超额**：`Q_mun_hp = min(Q_hp, Q_mun_demand) ≤ demand`，
   正常模式下不会超额交付，容差失去原用途
2. **它会让 `Q_dump ≡ 0` 不成立**：`cap = demand·(1+tol) + ind_cap` 时，
   取 `Q_hp = demand·(1+tol) + ind_cap` 会得到 `Q_dump = demand·tol > 0`

**处置**：`heat_tolerance` 删除，上限改为 `[ demand + ind_cap ] / COP`，执法为硬等式。

**逐情形确认不引入不可行**（`Q_hp ≤ demand + ind_cap`）：

| 情形 | `Q_mun_hp` | `Q_ind` | `Q_dump` |
|---|---|---|---|
| `Q_hp ≤ demand` | `Q_hp` | `0` | `0` ✓ |
| `demand < Q_hp ≤ demand + ind_cap` | `demand` | `Q_hp − demand ≤ ind_cap` | `0` ✓ |

两种情形都可行且 `Q_dump = 0`。✓

---

## AI4 · v1.8 初稿四式的论证修正（应 AH4）

### 我此前的论证过强

v1.8 初稿称四式在余热远超需求的画像下"**无解**"。AH4 给出的 `z = 0` 反例正确：

```
z = 0：Q_hp = Q_mun_hp = 1.5，Q_boiler = Q_ind = Q_dump = 0
       P_hp = 0.375，Q_unused = 3.01056 − 0.375×3 = 3.01056 − 1.125 = 1.88556
校验：Q_unused ≤ Q_rec_max·(1−0)  ✓
      Q_ind ≤ cap·1              ✓
      Q_dump ≤ Q_hp_max·1        ✓
      Q_boiler ≤ cap·0 = 0       ✓
```

**四式可满足，模型整体可行。** 由一个分支无解推出整体无解，是逻辑错误。

### 正确的表述

在 R-EAST 满载（`Q_hp_max = 4.0141`、市政峰值 `1.5`）下：

- **`z = 1` 分支不可行**：它要求"余热吸尽"（`Q_unused = 0`）且"产热全给市政"
  （`Q_ind = Q_dump = 0`），于是 `Q_hp = 4.0141 > demand = 1.5`，
  而交付恒等式不允许超额 → 该分支无解
- **`z = 0` 分支可行**（见上）
- 因此**模型整体可行**，且该画像本来就不需要锅炉（`Q_hp_max > demand`），
  取 `z = 0` 是正确的

### 那么为什么要改用 min/max 规则

不是因为"四式不可行"，而是三条更实在的理由：

**其一，四式需要每时段一个二进制变量。** 168 h 窗口 = 168 个额外二进制。
min/max 规则是纯线性约束，无二进制。

**其二，min/max 规则**自动**蕴含四式想表达的性质，且无需开关。**
验证——四式的核心意图是"不允许烧气同时向工业售热"：

```
min/max 规则下，Q_boiler > 0  ⟹  Q_hp < demand
                              ⟹  Q_mun_hp = min(Q_hp, demand) = Q_hp
                              ⟹  Q_ind = min(Q_hp − Q_mun_hp, cap) = min(0, cap) = 0
```

即 `Q_boiler > 0 ⟹ Q_ind = 0` **是 min() 结构的推论**，不是额外约束。
同时 `Q_boiler > 0 ⟹ Q_hp < demand ≤ demand`，热泵产出被全额吸收，
故 `Q_unused = Q_rec − P_hp(COP−1)` 由热泵出力决定，不需要额外约束。

**其三，`z=1` 分支的不可行本身是"错位表达"的症状。**
用"是否允许烧气"去表达"热量该给谁"，必然在某些参数区产生空分支。
分配规则的天然形式是等式，不是开关。

### 处置

- v1.8 §AH 的"初稿四式导致整体不可行"论证**作废**，改为本节表述
- **min/max 分配规则与需求侧上限继续采用**（用户已确认），
  但需求侧上限的性质按 §AI3 修正（独立约束，非冗余）

---

## 附：需要同步修改的文件

| 文件 | 修改点 |
|---|---|
| `configs/registry.yaml` | 拆为 `canonical_config_truths` 与 `derived_quantities`；
  `single_card_power_mw` 移入派生量（AI1） |
| `configs/system.yaml` | 删除 `heat_tolerance` 的引用（若存在于 thermal 段）（AI3） |
| `configs/economics.yaml` | `heat_acceptance.heat_tolerance` 删除；上限改为 `demand + ind_cap`（AI3） |
| `SPEC-REVISIONS-8.md` §AH | 数值表按 AI2 重算；"整体不可行"论证按 AI4 改写；
  需求侧上限按 AI3 改为独立约束（已在原文就地标注） |
| `src/aidc/model/thermal.py` | 分配规则注释更新；需求侧上限的说明改为"独立约束"；
  `Q_dump ≡ 0` 不变量；删除 tolerance 参数（AI3） |
| `src/aidc/model/dispatch.py` | 约束清单更新（AI3、AI4） |
| `tests/test_invariants.py` | `test_canonical_quantities_unique` 改为两类判定；
  `test_s_heat_objective_includes_boiler_cost` 的期望值按 AI2 修正；
  新增 `test_normal_mode_dump_is_zero`（AI3） |

---

## 给代码代理的确认

1. **AH1–AH4 全部成立**，其中 AH2 是纯算术错误（我把旧 B 的 `+80` 错配给 C，
   并错称 A/B 碳排相同），AH4 是论证过强（由一个分支无解推出整体无解）。
2. **min/max 分配规则继续采用**，需求侧上限**保留为独立约束**（§AI3），
   并顺带删除 `heat_tolerance`，使 `Q_dump ≡ 0` 成为正常模式的可测不变量。
3. AH1 的处置按你的建议：**只登记一个配置真源，MW 换算写成派生关系**，
   派生量独立成类、不再有 `authorized` 字段。
4. AH2 的三处期望值我已重算并会同步到测试；`Q_unused` 的公式也一并更正。
5. AH4 的正确表述已写入 §AI4：四式**不是整体不可行**，
   只是 `z=1` 分支在余热过剩区空置；改用 min/max 的真实理由是
   "无需二进制 + 性质由 min() 结构自动蕴含 + 开关表达错位"。
6. 本轮之后 M1 守门测试为 23 条。若再发现缺口，仍按原约定：
   **停下来报告，不要自行选择口径。**
