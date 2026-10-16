# SPEC 决策附录 · 第八批（v1.8）

本文件回应代码代理第三轮 M1 阻断（下称 AG1–AG2）。

**权威性**：`SPEC.md` < `SPEC-REVISIONS.md` < … < `SPEC-REVISIONS-7.md` < **本文件**。

**修订日期**：2026-10-12

**范围**：替换 v1.7 的反互补约束为正确的**优先序约束**（AG1）；
重新定义跨文件重复检测规则（AG2）。其余（γ 机制、能量上限、条件化不变量、
W2/W3、X3/X4、Z1/Z2、AA/AB/AC、AE 的其余部分）**均不变**。

---

## 总览

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| AG1 | 反互补约束未排除反例 B；文档论证与自身表格矛盾 | **规格错误（约束无效）** | AH 改为优先序约束 |
| AG1b | `S-heat` 目标漏锅炉成本，会选经济上荒谬的方案 | **规格错误** | AH3 修正目标 |
| AG2 | 跨文件检测按裸参数名一律拒绝，会拒绝合法配置 | 规则过粗 | AI 改为命名空间 + 授权覆盖 + 规范量登记 |

---

## AH · 优先序约束（应 AG1）

### 我的错误

v1.7 §AE「修正四」写了反互补约束：

```
Q_boiler(t) ≤ q_boiler_capacity · z(t)
Q_dump(t)   ≤ Q_hp_max(t) · ( 1 − z(t) )
```

并用"B 的 `Q_dump > 0`"论证 B 被排除。**但同一节的表格里 B 的 `Q_dump = 0`**
（B 把热量全给了工业，没有弃热）。论证与表格直接矛盾，约束实际什么都没排除：

```
B: Q_mun_hp = 0, Q_ind = 1, Q_dump = 0, Q_boiler = 0.8
取 z = 1：  0.8 ≤ 4 × 1          ✓
          0   ≤ Q_hp_max × 0     ✓        → B 可行
```

AG1 的第二点同样成立：仅把第二式改为限制 `Q_ind + Q_dump` 也不够。
取 `z = 1` 时 `Q_ind + Q_dump ≤ 0`，于是 `Q_mun_hp = Q_hp = 0`（关热泵），
`Q_unused = Q_rec`（全部余热未利用），`Q_boiler = 0.8`（锅炉供全部市政）——
依然可行，依然浪费。

### 正确的规则：直接定义分配（优先序，非"烧气前提"）

余热优先的本质是一条**分配规则**，不是"烧气的前提条件"。v1.7.1 试图用 `z` 开关
表达它，方向就错了——`z` 是"是否允许烧气"，而我们需要的是"热量该给谁"。

**最终规则（替换 v1.7 §AE「修正四」与 v1.8 初稿四式）**：

```
1. 热泵产热先满足市政需求，剩余再给工业，再剩余则弃热：

       Q_mun_hp(t) = min( Q_hp(t) , Q_mun_demand(t) )
       Q_ind(t)    = min( Q_hp(t) − Q_mun_hp(t) , Q_ind_capacity(t) )
       Q_dump(t)   = Q_hp(t) − Q_mun_hp(t) − Q_ind(t)          ≥ 0

2. 锅炉只补市政缺口：

       Q_boiler(t) = max( 0 , Q_mun_demand(t) − Q_hp(t) )

   （注：`heat_tolerance` 已删除，见 `SPEC-REVISIONS-9.md` §AI3）

3. 未被热泵吸取的余热：

       Q_unused(t) = Q_rec(t) − Q_low_used(t)                   ≥ 0
       Q_low_used(t) = P_hp(t)·( COP − 1 )
```

**三条约束的性质**：

- 分配规则（1）**优先市政**：`Q_mun_hp` 取 `Q_hp` 与需求的最小值，
  因此只要热泵有产出且市政有需求，就先给市政——这正是"余热优先供市政"的直接表达
- 锅炉规则（2）**只补缺口**：需求被热泵覆盖时锅炉恒为 0，无需任何开关
- **`Q_unused ≥ 0` 不要求余热被吸尽**：热泵可以低于其热源侧上限运行，
  这是"余热供大于求时可弃用"的正确表达——**这正是初稿四式的致命缺陷**

### 为什么初稿的四式错了（第三个反例）

v1.8 初稿的四式（含 `z_boiler`）在**余热远超需求**的画像下会直接不可行。

验算（R-EAST 满载）：`Q_hp_max = 0.9333 × 4.3008 = 4.0141 MW_th`，
市政峰值需求 `1.5 MW_th`。取 `z = 1` 时四式要求 `Q_dump = 0` 且 `Q_unused = 0`：

- 热泵须满吸余热 → `Q_hp = 4.0141`
- 产热须全部给市政 → 但市政只能接 1.5（加 tolerance 为 1.575）
- 剩余 `4.0141 − 1.575 = 2.44` 无处可去 → 要么弃热（违反 `Q_dump = 0`），要么不吸（违反 `Q_unused = 0`）

**无解。** 而在余热不足的画像（R-NW 满载 `Q_hp_max = 4.0141`、峰值需求 3.5）下，
`z = 1` 又确实需要锅炉，两种情形的要求互相矛盾。

**根因**：用"烧气的前提"去表达"热量该给谁"是错位的。
分配规则应当无条件成立，与锅炉是否运行无关。

### 逐例验算（规则 1–3 对全部反例）

沿用 AG1 的算例（R-EAST：`Q_mun_demand = 0.8`、工业售 80、锅炉 400）：

| 情形 | `Q_hp` | `Q_mun_hp` | `Q_ind` | `Q_dump` | `Q_boiler` | 结论 |
|---|---:|---:|---:|---:|---:|---|
| A | 1.0 | 0.8 | 0.2 | 0.0 | 0.0 | ✓ 规则直接给出 |
| B（热泵压到 0.8 供市政） | 0.8 | 0.8 | 0.0 | 0.0 | 0.0 | ✓ 规则给出；`Q_unused = Q_rec − (0.8/4)×3 = Q_rec − 0.6` |
| C（热泵停） | 0.0 | 0.0 | 0.0 | 0.0 | 0.8 | ✓ 可行；`Q_unused = Q_rec`（余热全废） |

**B 与 C 都不是"被排除"，而是"规则正确表达了它们"**：

- B 表示"热泵只运行到刚好覆盖市政"——这是合理的运行方式
  （在余热供大于求、工业无人要时就是最优）
- C 表示"热泵全停、余热全废、锅炉供全部"——这是**最差**的运行方式，
  不是不可行

**关键区别**：在 v1.7.1 的框架下我把 B 当作"必须排除的反例"，
是因为当时的目标函数会让 B **优于** A（`S-heat` 漏锅炉成本，
导致"烧气省余热给工业"看起来有利）。**A 之所以应当被选中，
靠的是经济性（§AH3 修正目标）与碳排，而不是靠排除 B。**

**所以真正需要修的是两件事，都不是约束**：

1. **`S-heat` 目标补锅炉成本**（§AH3）——修正前后对同一算例：

   | 情形 | 工业收入 | 锅炉成本 | 净收益（修正前 → 修正后） |
   |---|---:|---:|---|
   | A（`Q_ind=0.2`, `Q_boiler=0`） | 16 | 0 | +16 → **+16** |
   | B（`Q_hp=0.8` 只供市政，`Q_ind=0`） | 0 | 0 | 0 → **0** |
   | C（热泵停，`Q_boiler=0.8`） | 0 | 320 | +80 → **−320** |

   修正前 C 反而是最优（+80），这就是"烧气省余热"的荒谬之处；
   修正后 A 最优、B 次之、C 最差——**符合工程直觉，且不需要任何约束**。

2. **`S-carbon` 在碳因子相同时目标中性**：A 与 B 的碳排相同
   （都做到"余热未浪费"），C 因余热全废而碳排更高。中性可接受——
   A 与 B 都是合理的运行方式，由经济性区分。

**结论：不需要针对 B、C 的约束。** 规则 1–3 本身保证
"热泵产热优先供市政"，其余由目标函数决定。这比 v1.8 初稿的 `z` 开关更简单、
更接近物理，且不引入不可行。

### 需要保留的性质（已修正为分配规则恒等式）

原 `test_boiler_used_only_for_gap` 的断言应当改写为**分配规则本身的正确性**：

```
逐时段断言（对全部五个策略、全部画像）：
    Q_mun_hp(t) == min( Q_hp(t) , Q_mun_demand(t) )                        （规则 1）
    Q_ind(t)    == min( Q_hp(t) − Q_mun_hp(t) , Q_ind_capacity(t) )        （规则 1）
    Q_dump(t)   == Q_hp(t) − Q_mun_hp(t) − Q_ind(t)                        （规则 1）
    Q_boiler(t) == max( 0 , Q_mun_demand(t) − Q_hp(t) )                    （规则 2）
    Q_unused(t) == Q_rec(t) − P_hp(t)·(COP − 1)                            （规则 3）
    Q_dump(t) ≥ 0 且 Q_unused(t) ≥ 0
```

这是**恒等式校验**，不是"排除某类解"。它不依赖目标函数，对五个策略一致成立。

> ⚠️ **本节的 A/B/C 数值表已作废**，见 `SPEC-REVISIONS-9.md` §AI2。
> 旧表的 C 情形旧 S-heat 误写为 +80（实为 0，+80 属于旧表 B），
> 且"A 与 B 碳排相同"错误（实为 68.5 vs 90.0）。
> 另见 §AI4：本节的"初稿四式整体不可行"论证过强，正确表述在该节。

### 与热泵出力上限的一致性

规则 1 隐含 `Q_mun_hp ≤ Q_hp`，而 `Q_hp = COP·P_hp` 受热源侧与需求侧双上限约束
（见 `hp_power_limit`）。因此规则 1 与 §W2 的 `hp_power_limit` 不冲突：

- 热源侧上限 `P_hp ≤ Q_rec/(COP−1)` 限制热泵**能吸多少**
- 规则 1 决定**吸上来的热给谁**
- 需求侧上限 `P_hp ≤ [Q_mun_demand·(1+tol) + Q_ind_capacity]/COP` 限制**总量**，
  在规则 1 下自动满足（因为 `Q_mun_hp + Q_ind ≤ 需求 + 接纳上限`）

**注**：需求侧上限在规则 1 下已成为冗余约束，建议保留作为防御性冗余。

### 经济一致性检查（新增，须实现）

规则 1 与规则 2 合起来是**强制优先序**，它含有"市政需求必须由热泵优先承担"的意味。
在"热泵热比锅炉便宜"时这与经济最优一致；反之则强制使用更贵的热源。
新增校验：

```
max(price_buy) / COP  <  boiler_unit_cost        否则告警
```

当前取值：R-EAST 峰段 1150 / 4 = **287.5** < **400**（锅炉）✓

若标定后该关系反转（电价极高或气价极低），**必须在报告中说明**，
并考虑把规则 1 改为"软优先"（允许在电价尖峰时段让出市政份额给锅炉）。
**不得静默保留一个经济上不成立的硬规则。**

### AH3 · `S-heat` 目标的修正（应 AG1 的观察）

AG1 指出"该策略不优化锅炉成本"，这是对的，而且它本身就是个缺陷。

v1.2 §V3 定义 `S-heat` 为：

```
−Σ_t Q_ind(t)·Δt·price_ind + Σ_t Q_mun_unserved(t)·Δt·pen_mun_short + SLA_penalty
```

两个问题：

1. **漏锅炉成本**。于是"烧气把余热省给工业"看起来有利
   （A 为 +16 元，B 为 +80 元，**B 更优**——经济上荒谬）
2. **市政缺口项恒为 0**。v1.7 引入锅炉后，交付恒等式保证市政永无缺口，
   该项在正常模式下不再起作用

**修正后的 `S-heat` 目标**：

```
S-heat = − Σ_t [ Q_ind(t)·price_ind − Q_boiler(t)·boiler_unit_cost ] · Δt
         + SLA_penalty_week                （fault 模式下另含市政缺口罚项）
```

即"供热净收益 = 工业售热收入 − 备用热源燃料成本"。

**修正前后对比**（1 h，工业售价 80，锅炉 400）：

| 情形 | 工业收入 | 锅炉成本 | 修正前净收益 | 修正后净收益 |
|---|---:|---:|---:|---:|
| A（`Q_ind=0.2`, `Q_boiler=0`） | 16 | 0 | +16 | **+16** |
| C（热泵停，`Q_boiler=0.8`） | 0 | 320 | **+80 ← 最优** | **−320 ← 最差** |

修正前 C 之所以更优，正是因为锅炉成本不在目标里——**这是缺陷本身**。
修正后 A 严格优于 C（差 336 元），符合工程直觉。

**`S-carbon` 的中性仍然存在**（须记录）：当 `ci_boiler == ci_ind_substituted` 时，
A 与 B（热泵只供市政、余热未浪费）的碳排相同，碳目标无法区分二者。
**这可以接受**——两者都是"余热未被浪费"的合理运行方式，由经济性区分；
需要区分的只有 C（余热全废），而 C 的碳排本来就更高。
**因此不需要额外约束。**

---

## AI · 跨文件重复检测规则（应 AG2）

### 问题

v1.7 §AF 提出"同一参数名出现在多个配置文件时（除白名单）应报错"。
AG2 指出这会拒绝现有**合法**配置：

| 例子 | 为何合法 |
|---|---|
| `unit_cost_energy`、`gamma` 同时出现在 `chemistry_na.yaml` 与 `chemistry_li.yaml` | 属于**不同化学体系**，是不同参数，只是恰好同名 |
| `gpu_capacity_total`、`s1_reserved_fraction`、`ci_grid_mid` 同时出现在默认配置与地区覆盖配置 | 这是**已授权的覆盖机制** |

AG2 的结论正确：**同名不一定重复，异名也可能代表同一个量。**
（后者正是 v1.7 抓到的 `ci_boiler` vs `ci_kgco2_per_mwh_th`。）

因此检测必须基于**带命名空间的参数身份**与**显式的授权关系**，不能用裸名字。

### 规则一：参数身份 = （命名空间，路径）

**命名空间由文件角色决定**：

| 文件 | 命名空间 |
|---|---|
| `chemistry_na.yaml` | `chem.na` |
| `chemistry_li.yaml` | `chem.li` |
| `system.yaml` | `system` |
| `economics.yaml` | `econ` |
| `generation.yaml` | `gen` |
| `experiments.yaml` | `exp` |
| `regions/r_nw.yaml` | `region.R-NW`（依此类推） |
| `registry.yaml` | `registry`（元数据，不参与业务校验） |

**参数身份** = `(命名空间, 点分路径)`。

**判定规则**：

```
同一 (命名空间, 路径) 出现两次           → 错误（这就是 YAML 重复键，必须拒绝）
同一路径、不同命名空间                    → 默认合法
```

**为什么"不同命名空间"默认合法**：

- `(chem.na, cost.unit_cost_energy)` 与 `(chem.li, cost.unit_cost_energy)` 是**两个不同的量**
  （钠电与锂电的单位造价），同名是自然的
- `(region.R-NW, compute_override.gpu_capacity_total)` 与
  `(system, compute.gpu_capacity_total)` 路径本就不同（前者多一层 `_override`），
  不构成同名

**地区覆盖的命名约定**：地区文件对默认配置的覆盖**一律使用 `*_override` 前缀**
（`compute_override`、`electricity_override`、`carbon_override`、`resilience_override`），
因此路径天然与默认文件不同，不需要额外白名单。

**违规示例**：若某地区文件直接写 `compute: {gpu_capacity_total: 6144}`（无 `_override`），
则与 `(system, compute.gpu_capacity_total)` 同名——这仍**不报错**，
因为命名空间不同；但加载器应**优先取地区值并在日志中记录覆盖**，
且该键必须出现在 §规则二的授权清单中，否则报错。

### 规则二：授权覆盖清单

覆盖不是无限制的。**地区命名空间只被授权覆盖以下键**（其余键若被覆盖则报错）：

| 被覆盖的键（默认命名空间中的路径） | 授权给 | 说明 |
|---|---|---|
| `system.compute.gpu_capacity_total` | `region.*` | 集群规模可按画像调整 |
| `system.compute.s1_reserved_fraction` | `region.*` | S1 占比按画像调整 |
| `econ.carbon.ci_grid_mid` | `region.*` | 区域电网碳因子 |
| `econ.electricity.price_valley/flat/peak/sell` | `region.*` | 分时电价 |
| `system.storage_common.area_limit_m2` | `region.*` | 布置面积 |
| `system.storage_site_default.*` | `region.*` | 室内/户外与温度偏移 |
| `system.thermal.heat_price_mun/ind` | `region.*` | 供热结算价 |

**地区独占**（只允许在 `regions/*` 中定义，默认文件中不得出现）：
`climate.*`、`renewables.*`、`heat.*`、`flexible_task_ratio`、
`extreme_weather.*`、供热季月份。

**默认独占**（地区文件中不得覆盖）：所有 `chem.*`、`gen.*`、`exp.*`、
`econ.finance.*`、`econ.penalties.*`、`econ.resilience.*`、
`system.pue`、`system.solver`、`econ.heat_carbon.*`。

理由：这些是**全局方法设定**，按画像改动会让四画像不可比。

### 规则三：规范量登记（捕获"异名同量"）

名字检查无法捕获"同一个量用了两个名字"。因此维护一份**规范量清单**，
登记每个物理量的**唯一授权位置**与**禁止别名**。

存放于 `configs/registry.yaml`，示例条目：

```yaml
canonical_quantities:
  - quantity_id: boiler_carbon_intensity
    description: "备用热源单位供热碳排"
    authorized: "econ.heat_carbon.ci_boiler"
    forbidden_aliases:
      - "system.thermal.backup_heat.ci_kgco2_per_mwh_th"
    history: "v1.7 曾在两处定义（不同名），已收口"

  - quantity_id: industrial_heat_substituted_ci
    authorized: "econ.heat_carbon.ci_ind_substituted"
    forbidden_aliases:
      - "system.thermal.ci_ind_substituted"

  - quantity_id: heat_pump_cop
    authorized: "system.thermal.cop"
    forbidden_aliases: []

  - quantity_id: gpu_single_card_power
    authorized: "system.compute.gpu_power"
    forbidden_aliases:
      - "system.compute.gpu_power_kw_per_card"
```

**检测算法（完整）**：

```
1. 解析全部配置文件，得到 {(namespace, path): value} 与 (namespace, path) 的出现次数
2. 规则一：同一 (namespace, path) 出现 > 1 次          → 报错
3. 规则二：地区命名空间覆盖了未授权的默认键            → 报错
           地区命名空间定义了默认独占键               → 报错
           默认命名空间定义了地区独占键               → 报错
4. 规则三：任一 canonical_quantities 的 forbidden_aliases 被定义 → 报错
5. 反向检查：每个 quantity_id 的 authorized 位置必须存在 → 否则告警（登记过期）
6. 输出：解析到的 (namespace, path) 清单与覆盖关系日志，供人工复核
```

**明确不做**：不检查"同一裸参数名出现多次"。AG2 的两类合法情形
（不同化学体系同名、地区覆盖）都由命名空间天然区分。

### 新增测试

```
test_config_duplicate_keys_within_namespace   （规则一）
    对每个 (namespace, path) 断言出现次数为 1
    用自定义 loader 检测 YAML 重复键（PyYAML 会静默覆盖）

test_region_override_authorized               （规则二）
    对四画像逐一断言：地区文件覆盖的键全在授权清单内；
    未定义地区独占键之外的默认键；未覆盖默认独占键

test_canonical_quantities_unique              （规则三）
    断言任一 forbidden_aliases 均未被定义
    断言每个 authorized 位置存在（登记未过期）
    断言没有两个 quantity_id 指向同一位置
```

---

## 附：需要同步修改的文件

| 文件 | 修改点 |
|---|---|
| `SPEC-REVISIONS-7.md` §AE「修正四」 | **作废**，就地标注指向本文件 §AH |
| `SPEC-REVISIONS-7.md` §AF 的跨文件检测要求 | **作废**，就地标注指向本文件 §AI |
| `SPEC-REVISIONS-2.md` §V3 的策略目标表 | `S-heat` 目标按 §AH3 修正 |
| `src/aidc/model/thermal.py` | `allocate_heat` 的约束说明改为优先序四式；新增 `check_merit_order_economics` |
| `src/aidc/model/dispatch.py` | 约束清单更新为优先序四式；`z_boiler` 保留但语义改变 |
| `configs/registry.yaml` | **新建**：命名空间、授权覆盖清单、规范量登记 |
| `configs/regions/*.yaml` | 确认覆盖键全在授权清单内（当前已符合，无需改动） |
| `tests/test_invariants.py` | 更新 `test_boiler_used_only_for_gap`；新增 AI 的三条测试 |

---

## 给代码代理的确认

1. **AG1、AG2 全部成立**。AG1 的第一点尤其要紧：**我的文档论证与自己给出的表格矛盾**
   （说 B 有 `Q_dump > 0`，表格里 B 的 `Q_dump = 0`）。约束因此完全无效。
2. **采用你提出的优先序思路**，并形式化为单二进制四式（§AH）——

   ```
   Q_unused ≤ Q_rec_max · (1 − z)
   Q_ind    ≤ Q_ind_cap · (1 − z)
   Q_dump   ≤ Q_hp_max  · (1 − z)
   Q_boiler ≤ q_boiler_cap · z
   ```

   它同时排除 B 与你补的第二个反例（关热泵 + 锅炉供全部），
   而且不引入新的不可行情形（逐例验算见 §AH）。
3. 你指出"仅把第二式改为限制 `Q_ind + Q_dump` 仍不完整"完全正确——已按上述四式处理。
4. **并同步修正了 `S-heat` 的目标**（§AH3）：它漏锅炉成本，本身就会选 B。
   修正后 A 的净收益 +16 优于 B 的 −240，**即使无约束也会选 A**；
   优先序约束的作用是让 `S-carbon` 这类目标中性的策略也一致。
5. **AG2 的检测规则改为三层**（§AI）：命名空间 + 授权覆盖清单 + 规范量登记。
   你的两个例子都被明确允许：不同化学体系同名由命名空间区分；
   地区覆盖由 `*_override` 前缀与授权清单处理。**不做裸名检查。**
6. 你提醒"正常与故障模式还须遵守已有的电优先、允许失供规则"——已处理：
   fault 模式下市政交付恒等式放宽（允许 `unserved_heat`），
   优先序约束只作用于**实际交付**的热量，电优先规则不变。
7. 按本文件修正后即可继续 M1。若再发现缺口，仍按原约定：
   **停下来报告，不要自行选择口径。**
