# SPEC 决策附录 · 第三批（v1.3）

本文件回应代码代理第三轮复核提出的 4 处冲突（下称 S1–S4），给出最终口径。

**权威性**：`SPEC.md` < `SPEC-REVISIONS.md`(v1.1) < `SPEC-REVISIONS-2.md`(v1.2) < **本文件**。

**修订日期**：2026-10-10

---

## 总览

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| S1 | V1 充电公式与守恒测试冲突（γ² 损耗） | **规格错误（我引入的伪机制）** | W1 重写 |
| S2 | `gamma_eff` 时点方向前后不一 | 规格错误 | W1 一并删除 |
| S3 | `Q_dump` 混用两种不同的热 | 规格错误 | W2 拆分 |
| S4 | 24h 代表日容纳不下 24h 容忍窗口 | 规格错误 | W3 改为 168h 代表周 |

**关键判断**：S1 与 S2 的根源是我在 v1.2 里发明了 `gamma_eff` 这个机制。它是**多余的且错误的**——SOC 本身是守恒状态，只要状态守恒，就不存在"温度套利"，因为我当初担心的"自由能量"根本不成立。W1 删除该机制，回到更简单且正确的写法。

---

## W1 · γ 的正确用法（应 S1、S2）

### 为什么 v1.2 错了

v1.2 让 γ 进入 SOC 动态：

```
soc(t+1) = soc(t) + [ γ·η_ch·p_ch·Δt − p_dis·Δt/(γ·η_dis) ] / E_nom
```

S1 的验算正确：γ = 0.5、η = 1、E = 100、充电 1 MWh 时 Δsoc = 0.005；
若放电也用 γ = 0.5，只能放出 0.005 × 0.5 × 100 = **0.25 MWh**。
即充电 1 MWh 只能放出 0.25 MWh——**γ² = 0.25 的凭空损耗**。

γ 是**容量折减系数**，不是效率。把它写进动态等于宣称"低温下能量在往返中额外损失"，这与 γ 的物理含义（多少容量的能量可用）不符。

### 更根本的：我担心的"温度套利"不成立

v1.2 引入 `gamma_eff` 的理由是防止"低温充、高温放"获取净增益。**这个担心是错的**，理由：SOC 本身是守恒状态量，任何时刻放电量都不可能超过之前充入的总量加上初始储能。温度只影响**能从已有储能中取出多少**，不会创造能量。

举个具体验算：soc_ref = 0.5（即已有 50 MWh 储能，来自此前的充电），低温（γ = 0.5）充 20 MWh 到 soc = 0.7；升温（γ = 1）后放到 soc = 0.1，放出 60 MWh。
能量平衡：放出 60 − 充入 20 = 净放出 40 MWh；SOC 从 0.5 降到 0.1 = 40 MWh。**账目闭合，没有自由能量。** 初始的 50 MWh 储能是先前花钱充进去的。

因此 `gamma_eff` 不仅多余，还引入了 S1 的 γ² 损耗与 S2 的时点方向不一致。**删除。**

### 最终模型（替换 v1.2 的 §V1 全部内容）

#### 状态与动态（不含 γ）

```
状态：      soc_x(t) ∈ [ soc_min , soc_max ]            （标称窗口，恒定，恒可行）
动态：      soc_x(t+1) = soc_x(t) + [ η_ch_x(T)·e_ch_x(t) − e_dis_x(t)/η_dis_x(T) ] / E_nom_x
其中        e_ch_x(t)  = p_ch_x(t)·Δt     （母线侧充电能量，MWh）
            e_dis_x(t) = p_dis_x(t)·Δt    （母线侧放电能量，MWh）
```

**γ 不出现在动态中。** 这是与 v1.2 的根本区别。

#### 能量可用性约束（γ 在此作用）

> ⚠️ **本节公式已被 `SPEC-REVISIONS-4.md` §X3 取代**。v1.3 的原式漏了正部截断与
> 效率换算，会在越带时不可行、在 η < 1 时穿过可用下界。以 v1.4 的 X3 为准。

```
0 ≤ e_ch_x(t)  ≤ ( E_nom_x / η_ch_x(T) ) · [ soc_hi_x(T) − soc_x(t) ]⁺
0 ≤ e_dis_x(t) ≤   η_dis_x(T) · E_nom_x  · [ soc_x(t) − soc_lo_x(T) ]⁺
```

其中 `[x]⁺ = max(0, x)`，`e_ch = p_ch·Δt`、`e_dis = p_dis·Δt`（母线侧能量，MWh）。

**时点约定**：可用带在**时段起点 t** 求值，即用 `soc_hi(T(t))`。由此得到的
不变量是 `soc(t+1) ∈ [soc_lo(T(t)), soc_hi(T(t))]`——不是 `soc(t) ∈ band(T(t))`。
温度骤降时当前 SOC 高于 `soc_hi(T(t))` 是允许的（此时充电冻结为 0，而非不可行）。

以下为 v1.3 的原始表述，保留以供对照：

```
（作废）
可放出能量上限：  e_dis_x(t) ≤ E_nom_x × [ soc_x(t) − soc_lo_x(T_storage(t)) ]
可吸收能量上限：  e_ch_x(t)  ≤ E_nom_x × [ soc_hi_x(T_storage(t)) − soc_x(t) ]
```

其中温度相关的**可用边界**：

```
soc_hi_x(T) = soc_ref_x + γ_x(T) · half_band_x
soc_lo_x(T) = soc_ref_x − γ_x(T) · half_band_x

soc_ref_x    = soc_init_x                            （0.50）
half_band_x  = 0.5 · ( soc_max − soc_min )           （0.40）
```

**几何含义**：温度收缩可用 SOC 带，且**以 `soc_ref` 为中心对称收缩**。

- γ = 1.0 时：`[0.1, 0.9]`，与标称窗口完全一致
- γ = 0.5 时：`[0.3, 0.7]`，可用带宽 0.4（标称为 0.8），折减比例恰为 γ

**为什么要以 soc_ref 为中心**：保证初始状态 `soc_init` 永远落在带内。v1.1 的"收缩上界"（`soc_max·γ`）在 -25 ℃ 下给出 0.48 < 0.50 的不可行，正是因为收缩不以初始点为中心。

#### 功率上限（速率效应，与 γ 分离）

```
p_ch_max_x(t)  = min( P_nom_x , c_max_charge_x(T)·E_nom_x )
p_dis_max_x(t) = min( P_nom_x , c_max_discharge_x(T)·E_nom_x )
```

`c_max_charge` 内含 `low_temp_charge_margin`（低温充电的安全限制），调用方不再乘。

**两个机制的职责划分**（务必在实现中保持分离）：

- **γ 管容量**：低温下可用 SOC 带宽收窄 → 累计可放出/可吸收能量按 γ 折减
- **c_max 管速率**：低温下最大充放功率下降 → 逐时功率受限

### 为什么这个写法同时满足四项要求

**要求 1 · 累计可用能量被 γ 折减**

从 `soc_ref = 0.5` 出发，γ = 0.5、E_nom = 100 恒定：

```
soc_lo = 0.5 − 0.5×0.4 = 0.3
e_dis ≤ 100 × (0.5 − 0.3) = 20 MWh      ← 放电到 soc = 0.3
e_dis ≤ 100 × (0.3 − 0.3) = 0          ← 停止，无法继续
累计可放出 = 20 MWh = γ × 40 MWh       ✓
```

**这条修正直接回应 R1（第二轮）指出的几何级数问题**：R1 的推导针对的是"下界固定在 `soc_min`"的写法（那里会收敛到 40 MWh）；现在下界是温度相关的 `soc_lo(T) = 0.3`，级数在 20 MWh 处终止。**R1 的推理是对的，v1.1 的修法（逐时乘 γ）是错的，v1.3 的修法（温度相关下界）是对的。**

**要求 2 · 充 1 放 1，无 γ² 损耗**

γ = 0.5 恒定，E_nom = 100，η_ch = η_dis = 1，从 soc = 0.5 出发：

```
充电 1 MWh：Δsoc = +1/100 = +0.01        → soc = 0.51
            同时校验可用性：1 ≤ 100×(0.7 − 0.5) = 20  ✓
放电 1 MWh：Δsoc = −1/100 = −0.01        → soc = 0.50（复原）
            同时校验可用性：1 ≤ 100×(0.51 − 0.3) = 21 ✓
净效果：进出各 1 MWh，SOC 精确复原      ✓ 无 γ² 损耗
```

**注意**：由于 γ 不进入动态，SOC 的每 MWh 变化恒为 `1/E_nom`（η = 1 时），与温度无关。
γ 的作用体现在**允许的 SOC 幅度**上（`soc_lo` 到 `soc_hi` 只有 0.4 宽），
而不是每 MWh 的 SOC 变化率上。这是 v1.3 与 v1.2 的分野。

**要求 3 · 无温度套利**

见上文验算：SOC 是守恒状态，能量进出账目始终闭合。温度只影响"能从已有储能取出多少"，不创造能量。

**要求 4 · 恒可行**

状态边界是标称的 `[soc_min, soc_max]`，与温度无关；`soc_ref` 是可用带的中心，永远在带内。
温度骤降导致 `soc_hi` 低于当前 SOC 时，充电约束的右侧变为负数 → 充电流自动为 0，**不会不可行**（这是关键：约束加在能量流上，不是加在状态上）。

### 删除项

以下内容全部删除，不再需要：

- `gamma_eff()` 函数（`storage.py` 与 `chemistry.py` 中的两处）
- `soc_bounds()` 的"收缩上界"语义（改为返回 `soc_lo(T)` / `soc_hi(T)` 的可用带）
- v1.2 的 §V1 (a)(b)(c)(d) 全部四式
- `gamma_eff_Na` / `gamma_eff_Li` 轨迹列

### 验证测试（替换 v1.2 §V1 的三条测试）

```
test_cumulative_discharge_capacity_derated
    恒定 γ = 0.5、E_nom = 100、soc_ref = 0.5、η = 1
    断言：累计可放出能量 = 20 MWh（容差 1e-6），而非 40 MWh

test_charge_discharge_roundtrip_is_lossless
    恒定 γ = 0.5、η = 1
    断言：充 1 MWh 后再放 1 MWh，SOC 复原（容差 1e-9）
    即不存在 γ² 损耗

test_energy_balance_always_closes
    任意温度序列
    断言：Σ e_dis − Σ e_ch = E_nom · ( soc_start − soc_end )   （容差 1e-6，η = 1 时）

test_temperature_drop_does_not_make_infeasible
    构造：先在 γ = 1 时充到 soc = 0.8，再令温度骤降至 γ = 0.4
    断言：求解成功（不是 infeasible），且该时段 e_ch = 0

test_usable_band_scales_with_gamma
    断言：γ = 1 时可用带为 [0.1, 0.9]；γ = 0.5 时为 [0.3, 0.7]
    即带宽比例严格等于 γ
```

### 报告口径

γ 的物理含义统一表述为：

> 低温下电池的可用 SOC 带宽收窄为标称带宽的 γ 倍，且以标称工作点为中心。
> 这表现为"低温时既能充进去的更少、也能取出来的更少"，累计可用能量按 γ 折减。
> 本模型不刻画低温容量损失中的不可逆部分（析锂导致的活性物质损失），
> 该部分由 `c_max_charge` 与低温充电裕度间接表达。

---

## W2 · 余热与弃热的拆分（应 S3）

### 问题确认

S3 正确。v1.2 把两个不同位置的热混用 `Q_dump`：

1. **热源侧**：可回收的 IT 余热 `Q_rec` 中，未被热泵吸取的部分
2. **输出侧**：热泵产热 `Q_hp` 中，未被市政/工业接纳的部分

南方画像下 `P_hp = 0` → `Q_hp = 0` → 若守恒式为 `Q_hp = Q_mun + Q_ind + Q_dump`，则 `Q_dump` 必须为 0；但 v1.2 的文字又要求它等于 `Q_rec`。两者不可能同时成立。

### 最终模型（替换 v1.2 §V6 的热平衡部分）

拆为**两个独立的平衡式**：

#### 热源侧（低品位热）

```
Q_low_used_x(t) = P_hp(t) · ( COP − 1 )          [MW_th]   热泵从余热中实际吸取的热
Q_unused(t)     = Q_rec(t) − Q_low_used_x(t) ≥ 0  [MW_th]   未被利用的余热（排入环境）
约束：           Q_low_used_x(t) ≤ Q_rec(t)
```

`Q_unused` **不参与热泵产热的守恒式**，它表示"从未被热泵提走的那部分低品位热"。物理上去向是排入环境（由制冷系统承担），不进入供热分配。

#### 输出侧（热泵产热）

```
Q_hp(t) = COP · P_hp(t)                            [MW_th]
Q_hp(t) = Q_mun(t) + Q_ind(t) + Q_dump(t)          守恒式
Q_mun(t) ∈ [ Q_mun_demand(t) , Q_mun_demand(t)·(1+heat_tolerance) ]
Q_ind(t) ∈ [ 0 , Q_ind_capacity(t) ]
Q_dump(t) ≥ 0                                      热泵产热后未被接纳的部分
```

#### 两侧的连接：热泵出力的双侧上限

```
P_hp(t) ≤ min( Q_rec(t)/(COP−1) ,                                  ← 热源侧
               [ Q_mun_demand(t)·(1+tolerance) + Q_ind_capacity(t) ] / COP )   ← 需求侧
```

### 南方画像的正确行为（验算）

R-SOUTH：`Q_mun_demand = 0`、`Q_ind_capacity = 0`

```
需求侧上限 = 0/COP = 0  →  P_hp = 0
Q_low_used = 0          →  Q_unused = Q_rec（全部余热未被利用）
Q_hp = 0                →  Q_mun = Q_ind = Q_dump = 0   ✓ 守恒式成立
```

两个平衡式同时成立，不再矛盾。**这是正确的物理行为**：南方无供热需求，余热确实无处可用，不回收即最优。

### 变量与轨迹列

`TRAJECTORY_COLUMNS` 需包含：

```
Q_rec         可回收余热（MW_th）
Q_low_used    热泵从余热中吸取的热（MW_th）
Q_unused      未被利用的余热（MW_th）
Q_hp          热泵产热（MW_th）
Q_mun         市政供热（MW_th）
Q_ind         工业供热（MW_th）
Q_dump        热泵产热后未被接纳的部分（MW_th）
```

### 验证测试

```
test_heat_source_balance
    Q_rec(t) == Q_low_used(t) + Q_unused(t)          逐时段闭合

test_heat_output_balance
    Q_hp(t) == Q_mun(t) + Q_ind(t) + Q_dump(t)       逐时段闭合

test_zero_acceptance_implies_zero_heat_pump
    R-SOUTH 配置下断言 P_hp ≡ 0、Q_hp ≡ 0、Q_unused ≡ Q_rec

test_heat_pump_bounded_by_acceptance
    断言两条上限都成立（热源侧与需求侧取小）
```

### 碳抵扣口径（重申）

只有**实际被接纳**的热量计入替代减排：

```
carbon_avoid(t) = [ Q_mun(t) + Q_ind(t) ] · Δt · CI_substituted
```

`Q_dump` 与 `Q_unused` **不计入碳抵扣**——它们没有替代任何热源。

### 制冷与余热的耦合（明确简化）

`P_cool(t) = (PUE(t) − 1) · IT_load_total(t)`，由 IT 负荷与 PUE 决定，**不受热泵是否运行影响**。

**这是简化假设，必须在报告中说明**：真实系统中余热回收可能降低制冷负荷（例如用于预冷），本模型不刻画这一耦合。若后续要刻画，需在 §2.2 增加耦合项并说明依据。

---

## W3 · 低保真窗口改为 168 小时代表周（应 S4）

### 问题确认

S4 正确。v1.2 §V7 的硬要求是

```
arrive_h + duration_h + tolerance_h ≤ horizon_hours
```

而三个画像配置 `tolerance = 24`、`horizon = 24`，且 `arrive ≥ 0`、`duration > 0`，左侧必然 > 24。降低任务数无法解决——这是窗口长度与容忍窗口的结构性冲突。

### 最终方案：低保真改用 5 个 168 小时代表周

**放弃 24 小时代表日，改用 168 小时（一周）代表周。**

选中的代表周：

| ID | 代表周 | 覆盖年占比 |
|---|---|---|
| `W-WIN` | 冬季典型周 | 0.18 |
| `W-WINX` | 冬季极端周（全年最冷周） | 0.16 |
| `W-SPR` | 春季典型周 | 0.22 |
| `W-SUM` | 夏季典型周 | 0.22 |
| `W-AUT` | 秋季典型周 | 0.22 |

- 权重之和为 1.00；各周代表其相邻时段的年均特征，**权重需在结果中输出并记录**
- `W-WINX` 单列的理由：低温是本方案的核心机制，必须有足够的寒冷工况覆盖，否则低保真筛选会低估钠电在低温下的劣势

### 为什么这个改动同时解决多个问题

**其一，S4 直接消失。** 窗口 168 h，`tolerance = 24` 时约束变为 `arrive + duration + 24 ≤ 168`，即 `arrive ≤ 144 − duration`。对时长 2–8 h 的任务，到达时间上限在 136–142 h，**占总窗口的 81%–85%**，充足。

**其二，任务映射机制整个删掉。** v1.1/v1.2 的 `arrive_h % 24` 映射、跨日处理、容忍窗口截断规则全部不再需要——**低保真与主线实验使用完全相同的 168 h 窗口与同一个生成器**，只是运行 5 次而非全年。

**其三，一致性变好。** 低保真与高保真的任务模型、窗口长度、边界条件完全一致，多保真链路更可靠。

**其四，成本仍然可接受。** 5 × 168 = 840 h，相对全年 8760 h 约 10 倍加速，足够用于 BO 筛选。

### 代价与说明

放弃了原设计的"四季 × 三类天气"的天气类型覆盖（12 个 24 h 块）。作为补偿：

- 每个 168 h 周内自然包含天气变化与日夜循环
- `W-WINX` 提供了极端低温覆盖
- 若敏感性分析需要更细的天气分层，可在 M6 阶段增加额外的代表周，而不是回到 24 h 窗口

**报告中必须写明**：低保真使用 5 个代表周加权还原年均，代表周的选取规则与权重见表；这是一种**降采样**，与全年高保真结果可能存在差异，若差异显著以高保真为准并报告该差异。

### 配置修正

```yaml
fidelity:
  low:
    mode: "representative_weeks"
    weeks: [W-WIN, W-WINX, W-SPR, W-SUM, W-AUT]
    hours_per_week: 168
    total_hours: 840
    weights:
      W-WIN: 0.18
      W-WINX: 0.16
      W-SPR: 0.22
      W-SUM: 0.22
      W-AUT: 0.22
    # ⚠️ 权重之和必须为 1.0，加载时校验
    week_selection_rule: "各季节取最接近该季节中位工况的连续 168h；W-WINX 取全年 T_amb 最低点及其后连续 168h"
  high:
    mode: "full_year"
    hours: 8760
    include_resilience_scenarios: true
```

### 生成器的边界校验（保留，语义修正）

原 §V7 的校验仍然保留，但窗口改为 168 h：

```
断言（生成器自校验，硬要求）：
    ( tasks.arrive_h + tasks.duration_h <= horizon_hours ).all()
    ( tasks.latest_h <= horizon_hours ).all()

其中 latest_h = deadline_h + tolerance_h
```

**若某任务的容忍窗口导致越界**，生成器应**重新采样该任务的到达时间与截止时间**（往前放），而不是截断容忍窗口或丢弃任务。

**明确禁止**：缩短 `tolerance_h`、提前 `deadline_h`、截断 `duration_h`、丢弃任务。这四项都会改变任务集，破坏四臂可比性。

### 容忍窗口配置（保留）

```yaml
tasks:
  s3_tolerance_hours_default: 12.0
  s3_tolerance_hours_by_region:
    R-NW: 12.0
    R-WPV: 24.0
    R-EAST: 24.0        # 柔性低（0.45），需更长容忍窗口才有足够调度空间
    R-SOUTH: 24.0
```

168 h 窗口下这些取值全部合法（见上文的余量验算）。

---

## 附：需要同步修改的文件

| 文件 | 修改点 |
|---|---|
| `SPEC-REVISIONS-2.md` §V1 | **作废**，由本文件 W1 替换 |
| `SPEC-REVISIONS-2.md` §V2 | 循环公式简化为 SOC 掉幅（动态已无 γ 与 η 之外的因素） |
| `SPEC-REVISIONS-2.md` §V6 | 按 W2 拆分为两个平衡式 |
| `SPEC-REVISIONS-2.md` §V7 | **作废**，由本文件 W3 替换 |
| `src/aidc/model/storage.py` | 删除 `gamma_eff`；`soc_step` 去掉 γ 参数；`equivalent_cycles` 只保留 SOC 掉幅路径 |
| `src/aidc/data/chemistry.py` | 删除 `gamma_eff`；`soc_bounds` 改为返回温度相关可用带 `[soc_lo(T), soc_hi(T)]` |
| `src/aidc/model/thermal.py` | `allocate_heat` 拆为热源侧与输出侧两个平衡；新增 `Q_unused` |
| `src/aidc/model/dispatch.py` | 轨迹列改为含 `Q_rec`/`Q_low_used`/`Q_unused`/`Q_hp`；删除 `gamma_eff_*` |
| `tests/test_invariants.py` | 按 W1 的五条、W2 的四条重写相关测试 |
| `configs/experiments.yaml` | `fidelity.low` 改为 5 个代表周 |

---

## 给代码代理的确认

1. **S1–S4 全部成立**，其中 S1、S2 的根源是我在 v1.2 引入的 `gamma_eff` 伪机制，已整体删除。
2. 你关于"保留该动态就意味着额外的 γ 相关往返损耗；若坚持充入 1、放出 1，需要重新确定状态与可用容量的关系"的判断完全正确——最终采用的正是"重新确定状态与可用容量的关系"这条路线：**γ 作用在可用边界，不作用在状态动态**。
3. 你关于"分别记录未利用的低品位余热和热泵产热后的弃热"的建议被采纳，见 W2。
4. 按本文件修正后即可开始 M1。M1 阶段的守门测试为 W1 的五条 + W2 的四条，加原有的配置解析、单位换算、曲线结构与任务边界测试。
5. 若在实现中再发现冲突，仍按原约定：**停下来报告，不要自行选择实现方式**。
