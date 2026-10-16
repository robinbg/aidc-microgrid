# SPEC 决策附录 · 第七批（v1.7）

本文件回应代码代理 M1 阶段提出的 2 处阻断（下称 AD1–AD2）。

**权威性**：`SPEC.md` < `SPEC-REVISIONS.md` < `SPEC-REVISIONS-2.md` < `SPEC-REVISIONS-3.md`
< `SPEC-REVISIONS-4.md` < `SPEC-REVISIONS-5.md` < `SPEC-REVISIONS-6.md` < **本文件**。

**修订日期**：2026-10-12

**范围**：修正热侧的余热口径与供热需求标定（AD1）、修复 `generation.yaml`
的语法错误与重复键（AD2）。γ 机制、能量上限、条件化不变量、W2/W3、
X3/X4、Z1/Z2、AA/AB/AC **均不变**，但 AD1 会改变热平衡式与四份 region 的热参数。

---

## 总览

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| AD1 | 余热不足以满足 R-NW 的刚性供热需求 | **容量冲突（根因：热侧从未与系统规模校验）** | AE 修正余热口径 + 引入备用热源 + 重标定需求 |
| AD2 | `generation.yaml` 语法错误与重复键 | 文件错误 | AF 修复 |

---

## AE · 热侧的余热口径与备用热源（应 AD1）

### 根因

AD1 的计算完全正确，但根因比"余热不够"更基础：

```
IT 装机       = 6144 × 0.7 kW = 4.3008 MW
可回收余热上界 ≈ η_rec × IT_load（见下），即约 0.7 × 4.3008 = 3.01 MW_th（IT 侧）
而 R-NW 的市政峰值需求 = 8.0 MW_th
```

**8.0 MW_th 是我最初写配置时随手填的，从未与系统规模校验过。**
AIDC 的可回收余热上限由它的 IT 用电量决定——这是物理约束，增加储能或购电能力
都无法补足低品位热源。AD1 的这句判断准确。

这同时是 AB1 同类的失误：**我改了算力规模（AB1 的重定），却没有回头检查与它耦合的热侧**。

### 修正一：余热口径改为「IT 侧为主」（κ 参数）

原口径 `W_it = IT_load + P_cool` 把制冷电耗也算作可回收热。这在物理上偏乐观，
且引入一个虚假耦合：冷气候 → 低 PUE → 可用热更少，而冷气候同时又意味着供热需求更高。

**修正后的口径**：

```
W_it(t) = IT_load(t) + κ · P_cool(t)
```

- `κ = 0`（**默认**）：只计 IT 侧排热。对应主流做法——通过冷板液冷或背板换热器
  回收 40–60 ℃ 的中温热水；制冷机组（冷机/冷却塔）的排热温度更低，
  通常直排环境，不回收。
- `κ > 0`：表示额外回收制冷机组的冷凝热，需要专门的水冷式冷凝器设计。
  启用时**必须在报告中说明依据**，不得默认开启。

**副作用（正面）**：κ = 0 时余热供给**与 PUE 无关**，上述虚假耦合自动消除。

**可回收与可产热上界**：

```
Q_rec(t)    = η_rec · W_it(t) = η_rec · IT_load(t)
P_hp(t)     ≤ Q_rec(t) / (COP − 1)
Q_hp_max(t) = COP/(COP − 1) · η_rec · IT_load(t) = (4/3) × 0.7 × IT_load(t)
            = 0.9333 · IT_load(t)
```

**具体数值**（6144 卡，IT_base = 0.7182 MW，满载 4.3008 MW）：

| IT_load | Q_rec | Q_hp_max |
|---:|---:|---:|
| 0.7182 MW（仅基础负荷） | 0.5027 MW_th | 0.6703 MW_th |
| 2.052 MW（R-NW 平均，含任务） | 1.4364 MW_th | 1.9152 MW_th |
| 4.3008 MW（满载） | 3.0106 MW_th | 4.0141 MW_th |

**注意**：κ = 0 使供给比 AD1 按 PUE = 1.65 算出的 6.62 MW_th **更低**
（上限 4.01 MW_th）。这使需求重标定更加必要——不是可选优化。

### 修正二：引入备用（峰值）热源

**这是让"刚性需求"约束恒可行的正解**，也是区域供热的常规工程做法
（供热系统历来配峰值锅炉）。同时它使方案一贯主张的表述——
**"余热经热泵提温后作为补充热源，不是主要热源"**（SPEC §10.2）——
在模型里**字面成立**。此前这句话缺一个"主要热源"的角色，现在由锅炉承担。

**新增变量与约束**

```
备用热源出力：  Q_boiler(t) ∈ [ 0 , q_boiler_capacity ]

市政交付恒等式：Q_mun_hp(t) + Q_boiler(t) = Q_mun_demand(t)        （刚性，逐时成立）

热泵产出分配：  Q_hp(t) = Q_mun_hp(t) + Q_ind(t) + Q_dump(t)
```

- `Q_mun_hp(t)`：热泵供给市政的部分（**新增独立变量**，此前与 `Q_mun` 混用）
- `Q_boiler(t)`：备用热源补充的部分
- 网络侧需求 `Q_mun_demand(t)` 恒被满足（锅炉兜底），因此**永不可行性问题消失**

**供热优先顺序**（模型自动产生，无需额外规则）：
热泵热的边际成本 = 电价/COP ≈ 430/4 = **107.5 元/MWh_th**；
燃气锅炉 ≈ **400 元/MWh_th**。热泵显著更低，因此在**含成本项的目标**中，
优化器会倾向用尽可用余热。

**但仅有目标函数不足以保证余热优先**——必须补一条与目标无关的硬约束，
见下节的「反互补约束」。

### 修正四：~~反互补约束~~ → 见 SPEC-REVISIONS-8 §AH

> ⚠️ **本节已作废**。v1.7 的反互补约束（`Q_boiler ≤ cap·z`、`Q_dump ≤ Q_hp_max·(1−z)`）
> **无效**：反例 B 的 `Q_dump = 0`，取 `z = 1` 时两条约束都成立，B 未被排除。
> 本节原文用"B 的 `Q_dump > 0`"论证排除，与本节自己的表格矛盾。
> 正确规则为**优先序四式**，见 `SPEC-REVISIONS-8.md` §AH。
> 同时 `S-heat` 的目标需补锅炉成本，见该文件 §AH3。

**问题**：`allocate_heat` 的可行域允许"一边弃热、一边烧气"。用 R-EAST 当前参数
（`Q_hp = 1`、市政需求 `0.8`、工业售价 `80`、碳因子同为 `220`）构造反例：

| 分配 | `Q_mun_hp` | `Q_ind` | `Q_dump` | `Q_boiler` |
|---|---:|---:|---:|---:|
| A | 0.8 | 0.2 | 0.0 | 0.0 |
| B | 0.0 | 1.0 | 0.0 | 0.8 |

两种分配都满足输出侧守恒与市政交付恒等式。但各策略的表现不同：

- **`S-heat`**：目标含工业收入 `−Σ Q_ind·80`。A 为 −16 元，B 为 −80 元，
  **B 严格更优**。该策略的目标里没有锅炉成本，因此不会主动避免开锅炉。
- **`S-carbon`**：A 为 `0 − 0.2×220 = −44` kgCO₂，B 为 `0.8×220 − 1.0×220 = −44` kgCO₂，
  **两者等价**，无法区分。
- `S-cost` / `S-balance` / `S-base`：锅炉成本使 A 更优，但这是**策略目标的性质**，
  不是模型保证的性质。

**结论：市政余热优先必须是「全策略硬规则」，由约束保证，不能依赖目标函数。**

**处置**：新增反互补约束（二进制 `z(t)` 线性化"不许弃热的同时开锅炉"）：

```
Q_boiler(t) ≤ q_boiler_capacity · z(t)
Q_dump(t)   ≤ Q_hp_max(t) · ( 1 − z(t) )
z(t) ∈ {0, 1}
```

- B 被排除（`Q_dump > 0` 迫使 `z = 0`，进而 `Q_boiler = 0`，但 B 需要 `Q_boiler = 0.8`）
- A 仍可行（`Q_dump = 0` 允许 `z = 1`）
- 该约束**不引用任何策略目标**，因此对五个策略一致有效

**为什么这是正确的工程表达**：它正是"余热是补充热源、锅炉是峰值热源"这一原则的
模型化——有可回收余热可用时不应同时烧气。同时它使
`test_boiler_used_only_for_gap` 成为**模型保证的性质**，可以安全地保留为
全策略守门测试，无需收窄适用范围。

**代价与说明**：新增每时段一个二进制变量（168 个/窗口），对 MILP 规模影响很小。
报告中需说明该约束的存在及其工程含义。

**碳口径的已知简化**（须在报告中标明）：默认 `ci_boiler == ci_ind_substituted`，
此时市政与工业两条供热路径的碳排相等，**碳核算对"热量分配给谁"是中性的**，
分配由经济性与反互补约束决定。标定后若两者不同，需复核本结论。

---

**碳账口径（修正，避免重复计算）**

```
carbon_grass = g_imp · Δt · CI_grid  +  Q_boiler · Δt · CI_boiler
carbon_avoid = Q_ind · Δt · CI_ind_substituted
carbon_net   = carbon_grass − carbon_avoid
```

**关键**：市政侧的余热收益**不再作为"避免碳排"项**，而是体现为
`Q_boiler` 降低 → `carbon_grass` 降低。因为锅炉在系统边界内（scope 1），
用它自己减排更干净，也避免了"既算避免又算自身排放"的双重计算。

工业供热的减排在边界外（替代了外部热源），保留为 `carbon_avoid` 并标注口径。

`CI_substituted_heat` 的语义随之明确为 **被替代热源的碳强度**，
默认取 `CI_boiler`（220 kgCO₂/MWh_th，燃气锅炉），并注明替代的热源类型。

### 修正三：重标定供热需求

**注**：有了锅炉，需求的数值不再影响可行性，只影响**物理合理性与研究意义**。
标定原则：

1. 峰值需求控制在 AIDC 最大产热能力（4.01 MW_th）的 1–2 倍以内，
   使余热贡献占比有实质意义（目标 40%–65%）；
2. 需求与供热季、画像气候相匹配；
3. 工业供热的接纳能力同样下调——它与市政争同一份热量。

| 画像 | 市政峰值 (MW_th) | 市政基值 (MW_th) | 工业接纳上限 (MW_th) | 平均负荷下产热上界 / 峰值需求 |
|---|---:|---:|---:|---:|
| R-NW | 8.0 → **3.5** | 2.0 → **1.2** | 6.0 → **2.0** | 1.9152 / 3.5 = **54.7%** |
| R-WPV | 5.0 → **2.5** | 1.0 → **0.8** | 4.0 → **1.5** | 2.2344 / 2.5 = **89.4%** |
| R-EAST | 2.5 → **1.5** | 1.5 → **0.8** | 3.0 → **1.0** | 1.8246 / 1.5 = **121.6%** |
| R-SOUTH | 0.0 | 0.0 | 0.0 | —（无供热需求） |

口径说明（**必须随表报告**）：

```
平均 IT 负荷   = IT_base / ( 1 − R )        （由全系统电量口径反推）
Q_hp_max(平均) = 0.9333 × 平均 IT 负荷
比值           = Q_hp_max(平均) / 市政峰值需求
```

**⚠️ 这一列不是"余热贡献占比"，而是"潜在产热上界 ÷ 峰值需求"。**

- **可以超过 100%**（R-EAST 为 121.6%），含义是：在平均 IT 负荷下，
  产热上界已足以覆盖市政峰值。这不表示余热实际覆盖了全部峰值需求。
- **实际占比取决于调度**：热泵产热还要在市政、工业与弃热之间分配，
  且实际 IT 负荷随任务波动（上表用的是平均负荷）。
- **实际贡献占比只能在 M2 求解后从轨迹中统计**：

```
实际余热贡献占比 = Σ_t Q_mun_hp(t) / Σ_t Q_mun_demand(t)
```

该值必须从结果中计算并报告，**不得用本表的比值代替**。

标定的定量原则：使该比值落在 **50%–120%** 区间——过低说明余热对供热没有实质贡献，
过高（远超 100%）说明需求设得偏小、余热利用的价值被削弱。
R-EAST 的 121.6% 略高于区间上界，原因是它的 `1 − R = 0.55`（柔性低）
使平均 IT 负荷偏高；若标定后认为不合理，应调整 R-EAST 的市政峰值而非改动 IT_base。

**工业接纳上限为何也要下调**：`Q_mun_hp + Q_ind ≤ Q_hp`，两者共享同一份热量。
原值（R-NW 为 6.0）远大于最大产热能力，等于不构成约束。

### 新增配置

`configs/system.yaml` 的 `thermal` 段：

```yaml
thermal:
  cop: 4.0
  eta_recovery: 0.70
  kappa_cooling_heat: 0.0        # 制冷侧排热的可回收比例，默认 0（见 AE）
  # 备用热源（燃气锅炉，峰值兜底）
  backup_heat:
    q_capacity_mw_th: 4.0        # 可单独覆盖峰值需求
    thermal_efficiency: 0.90
    unit_cost_yuan_per_mwh_th: 400.0
  heat_price_mun: 50.0
  heat_price_ind: 80.0
```

**⚠️ 碳参数统一放 `economics.yaml`（唯一真源原则）**：`system.yaml` 与 `economics.yaml`
曾同时定义 `ci_ind_substituted`，属重复定义。已收口——碳相关的三个参数
（`ci_ind_substituted`、`city_heat_as_carbon_avoid`、`ci_boiler`）**只在
`economics.yaml` 的 `heat_carbon` 段定义**，`system.yaml` 仅留注释指向。

理由：同一参数两处定义时，加载顺序会静默决定取值。这与 AF 指出的**重复键**是
同一类风险——都不会报错。

各 `regions/*.yaml` 的 `heat` 段按上表更新，并删除 `system.yaml` 中
原 `q_mun_demand_mw_th: 6.2`（改为由画像提供必需值）。

### 新增测试

```
test_heat_supply_upper_bound
    断言 Q_hp(t) ≤ COP/(COP−1) · η_rec · (IT_load(t) + κ·P_cool(t))     逐时
    断言 Q_hp 与 PUE 无关（κ = 0 时改变 PUE 不改变上界）

test_municipal_demand_always_met
    断言 Q_mun_hp(t) + Q_boiler(t) == Q_mun_demand(t)                    逐时

test_heat_balance_full_window
    **逐时**校验热源侧与输出侧两式，并对整个窗口校验累计热量供需：
        Σ Q_mun_hp + Σ Q_boiler == Σ Q_mun_demand
        Σ Q_rec == Σ Q_low_used + Σ Q_unused
    （AD1 要求"同时检查逐时热功率与整个窗口的热量供需"）

test_boiler_used_only_for_gap
    断言在可用余热充足时 Q_boiler = 0（热泵边际成本低于锅炉）
    即锅炉不掩盖余热可回收量
```

---

## AF · `generation.yaml` 的语法修复（应 AD2）

AD2 指出的两处均确认，且**第 2 点的隐患比表面更严重**：

1. **第 159 行的折叠标量写法非法**：`note: > 正文` 中 `>` 后必须换行并缩进。
   `yaml.safe_load` 报 `ScannerError: expected a comment or a line break`。
2. **同一映射下 `note` 被重复定义**，PyYAML 会静默以后者覆盖前者。

**处置**：拆为两个不同的键，并改用普通字符串（避免折叠标量的换行陷阱）：

```yaml
  max_required_avg_utilization: 0.60
  precheck_note: "通过此预检不等于任务集一定可调度（仍需满足到达、截止、非抢占约束）；可调度性的真正验证在 M2 的求解阶段"

  flexible_ratio_tolerance: 0.05
  tolerance_note: "偏差超容差时应检查逼近逻辑，不得修改目标值"
```

**另需全局自查**：`configs/` 下所有 YAML 中的多行字符串一律改用
双引号单行形式或显式 `|` 块标量（`|` 与 `>` 后都必须换行缩进）。
`test_all_configs_parse` 是这道防线的守门测试，请确保它覆盖全部配置文件。

**教训**：这类错误由解析器直接暴露，成本低；但**重复键是静默的**——
PyYAML 不报错、后者覆盖前者。因此除解析测试外，建议加载器对每个配置文件
做一次**重复键检测**（用自定义 loader），避免同类问题在别处潜伏。

**同类风险已在别处发现并清理**（v1.7 修订时同步处理）：

| 重复定义的参数 | 原位置 | 处置 |
|---|---|---|
| `ci_ind_substituted` | `system.yaml` + `economics.yaml` | 只留 `economics.yaml` 的 `heat_carbon` |
| `ci_kgco2_per_mwh_th` | `system.yaml` 的 `backup_heat` + `economics.yaml` 的 `ci_boiler` | 只留 `economics.yaml` 的 `heat_carbon.ci_boiler` |
| `verify_municipal_heat` | `thermal.py` 两处函数定义 | 删除后一处（先定义者被覆盖） |

**跨文件重复定义**与 YAML 重复键是同一类风险，且同样静默。
加载层应做**跨文件同名参数检测**。

> ⚠️ **上述"按裸参数名检测"的要求已作废**，见 `SPEC-REVISIONS-8.md` §AI。
> 裸名检查会拒绝合法配置：`unit_cost_energy` 在钠电与锂电配置中同名是不同参数；
> 地区覆盖配置与默认配置同名是已授权机制。
> 正确规则为**命名空间 + 授权覆盖清单 + 规范量登记**三层，见该文件 §AI 与
> `configs/registry.yaml`。

README 中已加「参数唯一真源速查表」，实现时应以该表为准。

---

## 附：需要同步修改的文件

| 文件 | 修改点 |
|---|---|
| `configs/generation.yaml` | 修复 `note` 的语法错误与重复键（AF） |
| `configs/system.yaml` | `thermal` 段重写：加 `kappa_cooling_heat`、`backup_heat`；删 `q_mun_demand_mw_th`（AE） |
| `configs/economics.yaml` | `ci_substituted_heat` 语义改为"替代热源碳强度"，默认 220；`heat_acceptance` 的默认工业接纳上限下调（AE） |
| `configs/regions/*.yaml` | `heat` 段按 AE 的表格重标定；R-SOUTH 保持全 0 |
| `src/aidc/model/thermal.py` | `waste_heat` 加 `kappa` 参数；新增 `backup_heat_output`、`verify_municipal_delivery`（AE） |
| `src/aidc/model/dispatch.py` | 热平衡加 `Q_mun_hp`、`Q_boiler`；碳账按 AE 修正（AE） |
| `src/aidc/model/carbon.py` | `carbon_grass` 加锅炉项；市政余热不再计入 `carbon_avoid`（AE） |
| `tests/test_invariants.py` | 新增 AE 的四条测试；`test_all_configs_parse` 覆盖新字段 |

---

## 给代码代理的确认

1. **AD1、AD2 全部成立**。AD1 的计算准确，且**你的根因诊断比表面现象准确**：
   "增加储能或购电能力也无法补足低品位热源"——这句话点明了这是物理约束，
   不是优化空间问题。根因是我重定算力规模（AB1）时没有回头校验热侧，
   与 AB1 属同一类失误。
2. AD1 的处置按你给的两个方向**同时做**：
   - **校准需求**（8.0 → 3.5 MW_th 等，见 AE 表格）
   - **增加备用热源及其成本、碳排口径**（AE 的 `backup_heat`）
   单做任一个都不完整：只校准会让"刚性需求"约束仍然脆弱（余热随算力负荷波动），
   只加锅炉又会让需求数值继续不合理。
3. 你要求"修订时应同时检查逐时热功率与整个窗口的热量供需"——
   已写成 `test_heat_balance_full_window`，两个层级都断言。
4. 热平衡式因此改变（新增 `Q_mun_hp` 与 `Q_boiler`），但 W2 的**两侧拆分结构不变**：
   热源侧 `Q_rec = Q_low_used + Q_unused`，输出侧 `Q_hp = Q_mun_hp + Q_ind + Q_dump`。
5. AD2 除修复外，建议加载器增加**重复键检测**——PyYAML 对重复键是静默覆盖，
   不报错。这类问题不会自己暴露。
6. M1 守门测试更新为（共 18 条）：
   - `test_all_configs_parse`（含重复键检测）、`test_curve_blocks_have_points_key`、
     `test_curve_scale_range_present`
   - `test_unit_conversion_factor_applied`
   - `test_usable_band_scales_with_gamma`、`test_energy_limits_positive_part`、
     `test_bus_side_limits_respect_efficiency`
   - `test_flexible_ratio_matches_target`、`test_flexible_ratio_feasible_pretest`
   - `test_capacity_factor_matches_region_reference`
   - `test_representative_week_tasks_within_window`、
     `test_representative_week_weights_sum_to_one`
   - `test_extended_series_length`、`test_extreme_week_window_within_bounds`
   - **`test_heat_supply_upper_bound`、`test_municipal_demand_always_met`、
     `test_heat_balance_full_window`、`test_boiler_used_only_for_gap`（新增，AE）**
7. 若在实现中再发现缺口，仍按原约定：**停下来报告，不要自行补 YAML 或选择口径**。
