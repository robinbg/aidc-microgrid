> ## ⚠️ 本文件已作废 · DEPRECATED — 请勿据此实现
>
> **唯一权威规格是 [`SPEC.md`](./SPEC.md)（版本号见其标题行）。** 本文件是 v1.2 时期的历史留档，
> 其全部**有效**结论已并入 SPEC.md，其余内容**已被后续版本推翻**。
>
> ⚠️ **下方"权威性：`SPEC.md` < … < 本文件。冲突时以本文件为准"这句话已失效**——
> 那是 v1.2 当时的约定，现在**恰好相反**：冲突时一律以 `SPEC.md` 为准。
>
> 本文件含多处**已作废的错误结论**，照它实现会写出错的代码。
> 正确口径见 `SPEC.md` §2.3、§2.4、§4.2，变更缘由见 [`CHANGELOG.md`](./CHANGELOG.md)。

# SPEC 决策附录 · 第二批（v1.2）

本文件回应代码代理第二轮复核提出的 7 处冲突（下称 R1–R7），逐条给出唯一实现口径。

**权威性**：`SPEC.md` < `SPEC-REVISIONS.md`(v1.1) < **本文件**。冲突时以本文件为准。

**修订日期**：2026-10-10

---

## 总览

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| R1 | U4(b) 只限速率不限累计，γ 的容量折减实质未落实 | **规格错误（机制失效）** | V1 重写 |
| R2 | U6 循环公式与电池侧计量冲突，缺效率项 | 规格错误 | V2 修订 |
| R3 | U8 误判 SLA 惩罚为常数，且 U9 目标表未含该项 | **规格错误（推理错误）** | V3 修订 |
| R4 | U15 给出两套不等价 f3，参考值可能为零 | 规格错误 | V4 统一 |
| R5 | N-1 通过率依赖 U16 明确不计算的 T_island | 规格矛盾 | V5 选定 |
| R6 | S-base 热泵满出力在零接纳区违反热量守恒 | 规格错误 | V6 修订 |
| R7 | 代表日任务映射未保证合法 24h 窗口；缺 S3 容忍窗口 | 规格缺口 | V7 补充 |

R1 与 R3 是本次最值得重视的两条：R1 会让本方案的核心机制（低温容量折减）在长时程上被架空，R3 是我对问题性质的判断错误。

---

## V1 · 低温折减的守恒写法（应 R1）

### 问题确认

R1 的推理正确。设 `γ = 0.5`、SOC 下限以上有 40 MWh 可放，按 v1.1 的 (b)：

```
t1: e_dis ≤ 40 × 0.5 = 20  → 放出 20，剩余 20
t2: e_dis ≤ 20 × 0.5 = 10  → 放出 10，剩余 10
t3: e_dis ≤ 10 × 0.5 =  5  → 放出  5，剩余  5
...
累计 → 40 MWh（几何级数收敛于初始值本身）
```

**结论**：v1.1 的 (b) 只是逐时速率限制，长时程累计可用能量仍趋近全量，γ 的容量折减被架空。必须重写。

### 采用的方案：γ 只作用于「能量与母线的交换」

核心改动：**γ 不进入 SOC 状态定义，而是作为能量交换的效率因子**。低温下把电量充进电池，只能提高一半的 SOC（因为可用容量只有一半）；从电池取电时，也只能取出一半。

#### (a) SOC 动态（替换 v1.1 的 (c)）

```
soc_x(t+1) = soc_x(t) + [ gamma_eff_x(t)·eta_ch_x(T)·p_ch_x(t)·Δt
                        − p_dis_x(t)·Δt / ( gamma_eff_x(t)·eta_dis_x(T) ) ] / E_nom_x
```

其中

```
gamma_eff_x(t) = min( gamma_x(T_storage(t)) , gamma_x(T_storage(t+1)) )
```

`T_storage` 的求法见 U17（户外布置取环境温度；室内布置取环境温度加偏移并截断）。

- 充电：γ_eff 乘在分子（充进的能量只有 γ_eff 比例能真正存下来）
- 放电：γ_eff 乘在分母（取出的能量需要 1/γ_eff 倍的内部能量）
- **取相邻两时段 γ 的较小值**，用于消除"低温充、高温放"的非物理净增益（见下方测试 2）
- 初末 SOC 约束、SOC 窗口仍按名义容量

#### (b) 功率上限（保留 v1.1 的 (a)，去掉 γ 的重复作用）

```
p_ch_max_x(t)  = min( P_nom_x , c_max_charge_x(T)·E_nom_x )
p_dis_max_x(t) = min( P_nom_x , c_max_discharge_x(T)·E_nom_x )
```

**注意**：v1.1 在此处乘了 γ，现在**去掉**。理由：γ 已通过 (a) 的 SOC 动态作用，若再乘一次会双重折减。

`c_max_charge` 在 chemistry 模块内已含 `low_temp_charge_margin`，调用方不要再乘。

#### (c) 删除 v1.1 的 (b) 可用能量约束

SOC 上界由窗口本身给出，累计可用能量由 (a) 的 γ 因子隐式限制，不需要额外的逐时不等式。

### 验证（必须写成测试）

**测试 1 · 累计可用能量确实被折减**

```
设 E_nom = 100 MWh, soc_init = 0.5, soc_min = 0.1, γ = 0.5（恒定低温）, eta = 1
名义可放能量 = (0.5 − 0.1) × 100 = 40 MWh
期望实际可放能量 = 40 × γ = 20 MWh

按 (a) 放电式：Δsoc = p_dis·Δt / (γ·eta·E_nom) = p_dis / 50
若要 soc 从 0.5 降到 0.1（Δsoc = 0.4），需放出：
      0.4 × 50 = 20 MWh
断言：累计可放出能量 = 20 MWh（容差 1e-6），而非 40 MWh
```

**这条测试专防 R1 指出的失效模式**：若实现成"逐时可用能量乘 γ"，几何级数会让累计放出趋近 40 MWh，本断言会失败。

**测试 2 · 温度循环不产生自由能量（守恒律）**

v1.1/v1.2 初稿中此处的验算写得自相矛盾（同一场景一处算净增益、一处说被抑制）。**统一为下述唯一口径**：

```
场景：T_storage 由 -20℃ 升到 25℃，γ = 0.5 → 1.0，E_nom = 100，eta = 1
操作：低温充电 1 MWh，随后升温并全部放出

按 (a) 用 gamma_eff 取两时段最小值的写法：
  充电 1 MWh，gamma_eff = min(0.5, 1.0) = 0.5
      → Δsoc = +1/(0.5×100) = +0.02
  放电 Δsoc = 0.02，gamma_eff = min(1.0, 0.5) = 0.5（放电时段与上一时段取小）
      → 可放出 = 0.02 × (0.5×1.0×100) = 1 MWh
断言：净放出 = 1 MWh（容差 1e-6），既不放大也不缩小
```

**这是唯一正确的期望值**：能量既不能凭空放大（那会成为优化器可利用的套利），也不能凭空缩小到 0（那等于宣称低温充进去的能量在升温后消失了）。`gamma_eff` 取两端 γ 的较小值，恰好保证"进出对称"。

**测试 3 · gamma_eff 的取值方向正确**

```
构造温度由 25℃ 降到 -20℃（γ = 1.0 → 0.5）
断言：该时段的 gamma_eff = 0.5，而非 1.0
```

若不取小值而直接取当前时刻的 γ，升温时放电会放大能量（自由能量出现），降温时充电会低估容量。

### 关于 γ 的物理解释（写入报告）

本模型把低温折减解释为：**低温下电池的可用容量下降，表现为"充进去的能量只有一部分能存下来、取出的能量需要付出更多内部能量"**。

物理上低温容量损失包含可逆（极化增大）与不可逆（析锂导致的活性锂损失）两部分，本模型只刻画可逆部分，并额外用 `c_max_charge` 与 `low_temp_charge_margin` 表达低温充电的安全限制。

`gamma_eff` 取相邻两时段 γ 的较小值，是一项**保守化处理**：它禁止优化器利用"低温充、高温放"的温度差获取非物理的净增益，代价是略微低估升温时段的放电能力。在本方案的应用场景（低温是主要约束）下该保守性影响很小，且方向安全。

**报告中必须注明这是简化模型**。

---

## V2 · 循环计数的效率修正（应 R2）

R2 的推导正确。`p_dis` 是母线侧功率，电池内部消耗为 `p_dis / eta_dis`。计数必须与被 E_nom 归一化的量一致。

### 采用的公式

```
等效循环次数 = Σ_t [ p_dis_x(t) · Δt / ( gamma_eff_x(t) · eta_dis_x(T) ) ] / E_nom_x
```

与 SOC 动态的放电项一致：`

```
Δsoc_dis(t) = p_dis(t)·Δt / ( gamma_eff(t)·eta_dis(T)·E_nom )
```

因此更简洁的等价写法是**直接用 SOC 掉幅求和**：

```
等效循环次数 = Σ_t max( 0 , soc_x(t) − soc_x(t+1) )
```

（SOC 按名义容量的比例定义，因此 SOC 掉幅之和本身就是"等效满充放次数"。仅计放电造成的下降，充电的上升不计入。）

**推荐用 SOC 掉幅写法**：它与状态定义天然一致，不会出现口径漂移，且实现更简单。

**注意**：SOC 掉幅之和**不需要再除以任何量**——SOC 是无量纲比例，掉幅之和即循环次数的当量。这正是它与"母线侧功率求和"的差别所在。

### 接口修正

```python
def equivalent_cycles(
    soc: np.ndarray | None = None,          # 推荐：直接传 SOC 轨迹（长度 T+1）
    p_dis: np.ndarray | None = None,        # 备选：传放电功率
    T_storage: np.ndarray | None = None,    # 备选时必需（用于算 gamma_eff 与 eta）
    spec: StorageSpec | None = None,        # 备选时必需
    dt: float = 1.0,
) -> float:
    """等效循环次数（SPEC-REVISIONS §V2）。

    优先使用 SOC 轨迹：equivalent_cycles(soc=soc_traj)
        取 Σ_t (soc[t] − soc[t+1])⁺ ，即放电造成的 SOC 下降之和。

    备选（无 SOC 轨迹时）：equivalent_cycles(p_dis=..., T_storage=..., spec=...)
        Σ_t [ p_dis(t)·Δt / ( gamma_eff(t)·eta_dis(T) ) ] / E_nom
        ⚠️ 必须带 gamma_eff 与 eta_dis，否则与 SOC 动态口径不一致（R2 指出的错误）。

    不使用 /2。
    """
```

**测试**：两条路径对同一组输入必须给出相同结果（容差 1e-9）。

---

## V3 · SLA 惩罚的完整定义（应 R3）

### 我的判断错误，确认修正

R3 正确。v1.1 的 U8 写了"`SLA_penalty_week` 对给定任务集是常数，不改变同一配置下不同调度方案之间的排序"——**这是错的**。

迟到的任务数由调度决定（是决策变量），不是常数。同一配置下，"把 S3 推迟到低价时段"与"立即执行"会产生**不同的迟到任务数**，因此 SLA 惩罚项确实影响最优解。

**这条错误的影响**：v1.1 的措辞会让实现者认为该项可以随意处置（甚至移到求和外后忽略），从而漏掉真实的权衡。已修正。

### 唯一目标函数（替换 U8 与 U9 的全部相关表述）

每个策略只优化**一个**标量目标，**惩罚项与该策略目标的量纲必须一致**：

| 策略 | 目标（最小化） | 单位 | SLA 惩罚处置 |
|---|---|---|---|
| `S-cost` | Σ_t [购电成本 − 售电收入 − 工业供热收入 + 弃电惩罚] + `SLA_penalty_week` | 元 | **计入**（同为元） |
| `S-carbon` | Σ_t [g_imp·Δt·CI_grid − Q_accepted·Δt·CI_sub] | kgCO₂ | **不计入**；改为硬约束 `SLA_达标率 ≥ sla_floor`，见下 |
| `S-heat` | −Σ_t Q_ind(t)·Δt·price_ind + Σ_t Q_mun_unserved(t)·Δt·pen_mun_short + `SLA_penalty_week` | 元 | **计入**（同为元） |
| `S-balance` | 三项各自归一化后加权和 + `w_sla · (1 − SLA_达标率)` | 无量纲 | **计入**（归一化后） |
| `S-base` | 无优化，规则驱动 | — | 不适用 |

其中

```
SLA_penalty_week = ( 延迟任务数 / 总任务数 ) · pen_sla · MTok_week        [元]
```

- **在 Σ_t 之外，整窗口只计一次**（这一点 v1.1 是对的，保留）
- `延迟任务数` 是决策变量（由调度决定），不是常数
- `SLA_达标率` 定义见 U8：按时完成任务数 / 总任务数，分母只含 S2 与 S3

### `S-carbon` 为何把 SLA 改为约束而非惩罚

碳目标的单位是 kgCO₂，无法与元相加。若强行加一个惩罚系数把元转成碳，需要引入一个"碳的影子价格"，这个价格本身没有依据，会引入争议。

**处置**：`S-carbon` 使用硬约束

```
SLA_达标率 ≥ sla_floor        （sla_floor 在 experiments.yaml 中给出，默认 0.95）
```

若该约束导致不可行，则记录并报告（说明"纯碳优先策略在满足 SLA 底线的前提下不可行"），而不是放松约束。

### `S-balance` 的归一化

三项的量纲分别为 kgCO₂、元、元，必须先各自归一化到 [0, 1] 再加权：

```
x_i_normalized = ( x_i − x_i_min ) / ( x_i_max − x_i_min )
```

- 各 `x_min` / `x_max` 取自试点实验（如 50 次随机配置的评估范围），**固定后写入 config 并报告**
- 归一化范围固定后，所有实验复用，否则不同批次的"均衡"策略不可比
- `w_sla` 在 config 中给出，默认 0.3

### 报告口径（重申）

结果表必须同时给出：

- `objective_value`：该策略实际优化的目标（量纲随策略变化，**禁止跨策略比较**）
- `operating_cost_yuan`：统一口径的实际运营成本（Σ_t 部分，不含 SLA 惩罚）
- `sla_rate` 与 `delay_count`：SLA 相关质量指标

### 附：U7 与 U8 的关系澄清

R3 指出"截止后启动必然迟到"只是充分条件，本身不与 U8 冲突——**确认，接受**。统一按**完成时刻**判定是否迟到：

```
迟到 ⟺ 完成时刻 > deadline_h
```

启动时刻只影响可行性（受 `latest_h` 约束），不直接判定迟到。U7 的表述保持不变，此处仅澄清与 U8 的关系。

---

## V4 · f3 的唯一公式（应 R4）

v1.1 的 U15 确实给了两套不等价公式（`gap_cost / baseline` 与 EENS+孤岛加权式），且配置字段只支持第二套。**统一为一套。**

### 唯一公式（替换 U15 的全部 f3 表述）

```
f3 = w_eens · ( EENS / EENS_ref ) + w_island · max( 0 , 1 − T_island / T_island_ref )
```

- `EENS`：概率加权的期望未供能量（MWh）
- `T_island`：概率加权的孤岛支撑时长（h），把删失观测按窗口长度计入（下界）
- `w_eens = 0.5`、`w_island = 0.5`（config 可调，须报告）
- **热中断损失不进 f3**

### 热中断损失的处置

`heat_outage_cost_per_mwh` **保留但只用于旁路报告**，作为独立的韧性代价指标 `heat_outage_cost_yuan` 列在结果表中，不进入 f3。

理由：热中断损失的货币化系数（500 元/MWh_th）缺乏可靠依据，把它混入优化目标会让 f3 的数值高度依赖这个假设值。作为旁路指标报告则无此风险。

### 参考值：用配置常数，不用 A0 实际结果

**明确采用配置常数**（`eens_reference_mwh = 50.0`、`t_island_reference_hours = 12.0`），理由：

1. **A0 基线可能为零**。R4 指出的问题确实存在——A0 无储能，`T_island` 的储能支撑部分为 0，作分母会导致除零。
2. **可比性要求参考值在不同臂、不同地区、不同批次之间保持固定**。若取 A0 实际结果，则每次改变 A0 的配置都会改变所有 f3 的值，跨批次不可比。
3. 配置常数可以按地区覆盖（见下），语义清晰。

### 参考值的地区覆盖

若某地区的合理韧性尺度显著不同（如南方无供热、台风场景需要更长续航），可在 `regions/*.yaml` 中覆盖：

```yaml
resilience_override:
  eens_reference_mwh: 50.0
  t_island_reference_hours: 12.0
```

**默认值即为上述常数**，不需要各画像重复声明。覆盖时必须报告。

### 零参考值的防呆

`eens_reference_mwh` 与 `t_island_reference_hours` **必须 > 0**，在 config 加载时校验：

```python
assert cfg["resilience"]["eens_reference_mwh"] > 0
assert cfg["resilience"]["t_island_reference_hours"] > 0
```

若 `EENS` 为 0（无失供），`f3` 的第一项为 0，这是合法的（韧性完美）；若 `T_island` 超过参考值，第二项取 0（不再是负贡献）。

### 低保真阶段的 f3

维持 v1.1 的处置：**低保真阶段 `f3 = NaN`，BO 在三维目标上搜索；高保真确认阶段补齐 f3 并重新过滤 Pareto 前沿。**

---

## V5 · N-1 通过率的判定（应 R5）

R5 指出的矛盾成立。**选定方案：按关键负荷完整满足判定，同时增加组合场景。**

### N-1 通过判定（不使用 T_island）

```
N-1 通过 ⟺ 在整个观察窗口内 unserved_elec(t) = 0 且 unserved_heat(t) = 0
```

- **不使用 `n1_pass_threshold_hours`**。该字段删除（或改为 `n1_observation_hours`，表示观察窗口长度）。
- 观察窗口：取该场景的持续时间（N-1 为整个评估窗口，建议 24 h）。
- `n1_pass_rate` = 通过的 N-1 场景数 / N-1 场景总数。

### 新增组合场景 N-1 + 失电

补充一个**同时发生**的场景，标记为 `N1+G`：

```
单一储能单元故障（N-1）且同时电网失电
```

该场景**切断电网**，因此**报告 T_island**，并可沿用 `t_island_reference_hours` 作为评估尺度。

这是实际中最危险的工况组合（储能本已缺一半，电网又不可用），也是最有区分度的场景。

### 场景与指标的对应关系（唯一口径）

| 场景 | 是否切电网 | 报告 T_island | 报告 EENS | 计入 n1_pass_rate |
|---|---|---|---|---|
| G2 / G6 / G12 | 是 | ✓ | ✓ | ✗ |
| G24（长时） | 是 | ✓ | ✓ | ✗ |
| R-drop | 否 | ✗ | ✓ | ✗ |
| **R-drop + G**（组合） | 是 | ✓ | ✓ | ✗ |
| N-1 | 否 | ✗ | ✓ | ✓（按完整满足判定） |
| **N-1 + G**（组合） | 是 | ✓ | ✓ | ✗ |

`scenarios_summary` 的输出表必须包含"是否报告 T_island"与"是否计入 n1_pass_rate"两列，便于复核。

### 配置修正

```yaml
resilience:
  # 删除 n1_pass_threshold_hours
  n1_observation_hours: 24.0        # N-1 场景的观察窗口长度
  n1_pass_requires_full_service: true
  # 新增组合场景开关
  combined_scenarios:
    renewable_drop_plus_grid_outage: true
    n1_plus_grid_outage: true
```

---

## V6 · S-base 的热平衡修正（应 R6）

R6 指出的问题成立：南方的市政与工业接纳量都为零，若热泵仍按余热上限满出力，热量无处可去，热量分配等式无法满足。

### 修正一：热泵出力受接纳能力限制

```
P_hp(t) ≤ min( Q_rec(t)/(COP−1) , [ Q_mun_demand(t)·(1+tolerance) + Q_ind_capacity(t) ] / COP )
```

即热泵出力同时受**热源侧**（可回收余热）与**需求侧**（可接纳热量）约束。这是唯一正确的写法——热泵不是必须满出力。

### 修正二：增加弃热通道（可选，用于诊断）

```
Q_dump(t) ≥ 0        # 排空/弃热（MW_th）
Q_hp(t) = Q_mun(t) + Q_ind(t) + Q_dump(t)
```

- `Q_dump` 在**正常调度**中的最优值应为 0（有成本或至少无收益），它的作用是**显式暴露热量守恒**，而不是暗中丢弃。
- 若允许弃热，需计入轻微惩罚 `pen_dump`（元/MWh_th，默认取 0，仅用于打破并列解）或在报告中统计其总量。
- **推荐实现**：允许 `Q_dump` 但设 `pen_dump = 0`，并在报告中报告 `Σ_t Q_dump(t)`。若该值在正常调度中显著非零，说明系统存在"余热无法消纳"的结构性问题，值得报告。

### 修正三：S-base 的规则表述

替换 v1.1 中"S-base 热泵按可回收余热上限满出力"：

```
S-base 供热规则（修正版）：
  热泵出力 = min( 可回收余热对应的出力上限 ,
                  实际接纳能力对应的出力上限 )      ← 取小值
  先满足市政刚性需求（受 tolerance 上界约束）
  剩余给工业供热（受 Q_ind_capacity 约束）
  仍有余量则计入 Q_dump
```

### 修正四：南方画像的正确行为

R-SOUTH 下 `Q_mun_demand = 0`、`Q_ind_capacity = 0`，因此热泵出力上限为 0，`P_hp = 0`，余热不回收，`Q_dump` 等于全部可回收余热（计入报告）。

这是**正确的物理行为**——南方无供热需求，余热确实无处可用，"不回收"就是最优解。该画像预期给出"锂电为主"的结论，与此一致。

**⚠️ 但这带来一个次生问题：热泵不运行意味着 IT 排热未经热泵回收，那部分热量只能通过制冷系统排入环境。**

这需要与 `P_cool` 的建模保持一致。**处置**：`P_cool` 的计算为

```
P_cool(t) = (PUE(t) − 1) · IT_load_total(t)
```

即制冷能耗由 IT 负荷与 PUE 决定，**不受热泵是否运行的影响**。热泵回收的是"本会排入环境的热量"的一部分，回收与否不影响制冷能耗。

这是简化假设，需在报告中说明：**真实系统中余热回收可能降低制冷负荷（若用于预冷等），本模型不刻画这一耦合**。若后续要刻画，需在 §2.2 增加耦合项，并说明依据。

---

## V7 · 代表日任务映射与 S3 容忍窗口（应 R7）

### 问题确认

R7 正确。`arrive_h % 24` 不保证映射后的任务能在 24 小时内完成。例如 `arrive_h = 23, duration = 2` 映射后需要时段 [23, 24]，越界。

### 原则：不截断，重新生成

**明确禁止**：不得截断任务时长、不得提前 deadline、不得丢弃无法容纳的任务。这些都会改变任务集，破坏四臂可比性。

### 采用方案：代表日任务在 24 小时窗口内独立生成

低保真阶段**不为代表日映射周任务，而是用同一组生成参数、在 24 小时窗口内重新生成一组任务**：

```
generate_tasks(
    n_tasks         = ceil( n_week_tasks × 24 / 168 ),   # 按窗口长度等比例缩放
    horizon_hours   = 24,
    ...
    seed            = 同一 seed,
)
```

生成器需保证（V7 新增的硬要求）：

```
arrive_h + duration_h + tolerance_used ≤ horizon_hours
```

即**所有任务的到达、时长与容忍窗口之和必须落在窗口内**。生成后校验：

```python
assert (tasks.arrive_h + tasks.duration_h) <= horizon_hours, "任务越界"
assert (tasks.latest_h) <= horizon_hours, "容忍窗口越界"
```

### `flexible_task_ratio` 在低保真下的校验方式

原 U17 要求"比较生成结果的柔性占比与画像设定"。低保真下任务数按比例缩放，占比校验同样适用：

- 任务数减少会导致占比的采样方差变大
- **处置**：放宽校验容差（24h 窗口下允许 ±0.15），并在报告中记录实际占比
- 若偏差持续超过容差，需检查生成器逻辑而非调参

### S3 容忍窗口的配置（R7 指出的缺失项）

`experiments.yaml` 与各 `regions/*.yaml` 需补充：

```yaml
# experiments.yaml（全局默认）
tasks:
  s3_tolerance_hours_default: 12.0
  s3_tolerance_hours_by_region:
    R-NW:    12.0      # 风电反调峰，夜间出力高，短容忍即可
    R-WPV:   24.0      # 光伏日间尖峰，容忍窗口长有利于日间偏移
    R-EAST:  24.0      # 柔性低（在线业务多），长容忍以保持可行性
    R-SOUTH: 24.0
  # 可被 regions/*.yaml 覆盖
```

**取值依据**：容忍窗口应与该画像的 `flexible_task_ratio` 相称——柔性低的画像（R-EAST 为 0.45）需要更长的容忍窗口才能产生足够的可调度空间，否则调度问题容易不可行。

**约束**：`s3_tolerance_hours` 不得使任务越过窗口边界（见上）与任务越过 `latest_h`。

### 代表日与高保真的一致性（重申）

低保证与高保真**使用同一份生成参数与种子**，仅窗口长度与任务数按比例不同。这样低保真筛选出的配置在高保真下也应表现良好。

若两者结论系统性不一致，以高保真为准，并在报告中说明——**这是值得报告的发现，不是需要掩盖的偏差**。

---

## 附：需要同步修改的文件

| 文件 | 修改点 |
|---|---|
| `SPEC-REVISIONS.md` §U4 | 按 V1 重写：γ 进入 SOC 动态、加 `gamma_eff` 取小值、删除可用能量不等式 |
| `SPEC-REVISIONS.md` §U6 | 按 V2 修正循环公式与接口 |
| `SPEC-REVISIONS.md` §U8 | 按 V3 修正 SLA 惩罚的性质（非统计量但影响最优解） |
| `SPEC-REVISIONS.md` §U9 | 按 V3 的目标函数表统一（含 SLA 项与量纲处置） |
| `SPEC-REVISIONS.md` §U15 | 按 V4 统一 f3、按 V5 修正 N-1 |
| `SPEC-REVISIONS.md` §U16 | 按 V5 补场景-指标对应表 |
| `SPEC-REVISIONS.md` §U17 | 按 V7 修正代表日任务生成规则 |
| `configs/economics.yaml` | 删 `n1_pass_threshold_hours`；加 `n1_observation_hours`、`combined_scenarios`、f3 参考值防呆注释 |
| `configs/experiments.yaml` | 加 `tasks.s3_tolerance_hours_*`、`S-carbon` 的 `sla_floor`、`S-balance` 的归一化范围与 `w_sla` |
| `configs/regions/*.yaml` | 按 V7 补充容忍窗口覆盖（如需） |
| `src/aidc/model/storage.py` | `equivalent_cycles` 改为 SOC 掉幅优先；新增 `gamma_eff` 辅助函数 |
| `src/aidc/data/chemistry.py` | 删 `soc_bounds` 的收缩逻辑说明；新增 `gamma_eff(t)` |
| `src/aidc/data/tasks.py` | `generate_tasks` 增加窗口内越界校验（V7） |
| `src/aidc/model/dispatch.py` | 加 `Q_dump` 变量与热泵出力双侧约束（V6） |
| `src/aidc/resilience/scenarios.py` | 加组合场景 `R-drop+G`、`N1+G`（V5） |
| `src/aidc/resilience/metrics.py` | N-1 判定改为完整满足（V5）；f3 按 V4 公式 |

---

## 给代码代理的确认

1. **R1–R7 全部成立**，其中 R1（机制失效）与 R3（推理错误）是 v1.1 的实质缺陷，感谢指出。
2. 旧 `chemistry.py` 中"收缩 SOC 上界"的说明已被 U4 与 V1 覆盖，可同步删除——你的判断正确。
3. 按本文件修正后即可开始 M1。M1 阶段需新增以下守门测试：
   - `test_cumulative_discharge_capacity_derated`（V1 测试 1）
   - `test_no_free_energy_from_temperature_cycling`（V1 测试 3）
   - `test_equivalent_cycles_two_paths_agree`（V2）
   - `test_heat_pump_bounded_by_acceptance`（V6）
   - `test_representative_day_tasks_within_window`（V7）
4. 若在实现中再发现冲突，仍按原要求：**停下来报告，不要自行选择实现方式**。
