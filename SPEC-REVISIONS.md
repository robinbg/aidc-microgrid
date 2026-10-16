> ## ⚠️ 本文件已作废 · DEPRECATED — 请勿据此实现
>
> **唯一权威规格是 [`SPEC.md`](./SPEC.md)（版本号见其标题行）。** 本文件是 v1.1 时期的历史留档，
> 其全部**有效**结论已并入 SPEC.md，其余内容**已被后续版本推翻**。
>
> ⚠️ **下方"权威性：本文件与 SPEC.md 冲突时，以本文件为准"这句话已失效**——
> 它是 v1.1 当时的约定，现在**恰好相反**：冲突时一律以 `SPEC.md` 为准。
>
> 本文件含多处**已作废的错误结论**，照它实现会写出错的代码，例如：
> "γ 进入 SOC 动态"（会产生 γ² 的凭空往返损耗）、"反互补约束"、
> "`z_boiler` 四式"、"24 h 代表日"（容纳不下 S3 的 24 h 容忍窗口）、
> "取相邻两时段 γ 的较小值（`gamma_eff`）"。
> 正确口径见 `SPEC.md` §2.3、§2.4、§4.2，变更缘由见 [`CHANGELOG.md`](./CHANGELOG.md)。

# SPEC 决策附录（v1.1）

本文件是对 `SPEC.md` v1.0 的修订与补充，逐条回应代码代理的 20 条复核意见（下称 Q1–Q20）。

**权威性**：本文件与 `SPEC.md` 冲突时，以本文件为准。`SPEC.md` 的编号体系保持不变，本文件用 `U<n>` 编号，Q↔U 对应关系在各条开头标明。

**修订日期**：2026-10-10

---

## 总览

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| Q1 | 单位禁令过宽 + 成本公式缺换算 | 规格错误 | U1 修订 |
| Q2 | 三份 YAML 语法错误 | 文件错误 | U2 已修 |
| Q3 | 曲线类参数未落实区间 | 规格缺口 | U3 补充 |
| Q4 | 低温 SOC 边界公式未定，会直接不可行 | **规格错误** | U4 修订 |
| Q5 | 额定功率未进入约束 | 规格缺口 | U5 补充 |
| Q6 | 循环/衰减/替换成本口径未闭合 | 规格缺口 | U6 补充 |
| Q7 | S3 软截止与任务约束冲突 | **规格错误** | U7 修订 |
| Q8 | SLA 惩罚在逐时求和中被累计 | **规格错误** | U8 修订 |
| Q9 | 五策略不能仅靠权重实现 | 规格缺口 | U9 补充 |
| Q10 | S1 并入基础负荷后 GPU/Token 核算断链 | 规格缺口 | U10 补充 |
| Q11 | A0 的 LCOE 分母为零 | **规格错误** | U11 修订 |
| Q12 | 供热缺少接纳上界 | 规格缺口 | U12 补充 |
| Q13 | 碳公式漏 Δt、缺制造碳开关 | 规格错误 | U13 修订 |
| Q14 | 故障模型不能共用正常约束 | **规格错误** | U14 修订 |
| Q15 | 韧性策略缺必要参数 | 规格缺口 | U15 补充 |
| Q16 | T_island 观察边界不明 | 规格缺口 | U16 补充 |
| Q17 | 多保真/地区生成/BO 对照口径未统一 | 规格缺口 | U17 补充 |
| Q18 | 接口与 milestone 依赖有缺口 | 规格缺口 | U18 修订 |
| Q19 | 验收条件预设了结果 | **规格错误（立场问题）** | U19 修订 |
| Q20 | 严格确定性条件不完整 | 规格缺口 | U20 补充 |

Q19 单独说明：这不是技术歧义，是**立场错误**。我在验收条件里写了"必须出现结论翻转""分层配置应同时改善三项"，这与 SPEC §1.4「不预设钠电优势」直接矛盾。已按 U19 改正——把"必须观察到某结论"改成"若未观察到，须给出归因说明"。

---

## U1 · 单位换算（应 Q1）

### 内部单位集合（唯一允许在公式中直接使用的单位）

| 类别 | 内部单位 |
|---|---|
| 电功率 | MW |
| 电能量 | MWh |
| 热功率 | MW_th |
| 热能量 | MWh_th |
| 电价 | 元/MWh |
| 电网碳因子 | kgCO₂/MWh |
| 热源碳因子 | kgCO₂/MWh_th |
| 功率造价 | 元/MW |
| 能量造价 | 元/MWh |
| 运维 | 元/MWh/年 |
| 占地 | m²/MWh |
| 制造碳足迹 | kgCO₂/MWh |
| 倍率 | 1/h |
| 单卡功率 | MW/卡 |
| 时间 | h |
| 温度 | ℃ |
| 算力 | GPU·h |
| Token | MTok（百万 Token） |
| 成本 | 元 |

**替换 SPEC §1.1 的禁令原文**。原文"禁止 MW·h 之外的派生单位"表述有误，且与实际要求冲突。正确表述：

> §1.1 所有**公式内部**的量必须使用上表列出的内部单位。
> 配置文件允许使用行业惯用单位（元/kWh、元/kW、m²/kWh、kW/卡、kgCO₂/kWh），
> 但**加载时必须由 `chemistry.py` / 配置加载层统一换算**，换算后方可进入公式。
> 禁止在未换算的情况下直接把配置数值代入公式。

### 换算表（配置单位 → 内部单位）

| 配置单位 | 内部单位 | 换算因子 |
|---|---|---|
| 元/kWh | 元/MWh | × 1000 |
| 元/kW | 元/MW | × 1000 |
| 元/kWh/年 | 元/MWh/年 | × 1000 |
| m²/kWh | m²/MWh | × 1000 |
| kgCO₂/kWh | kgCO₂/MWh | × 1000 |
| kW/卡 | MW/卡 | ÷ 1000 |

**验收（必须实现为测试）**：

```python
def test_unit_conversion_factor_applied():
    """等价换算检验：把单位造价从元/kWh 改为等值的元/MWh 表达，
    即 mid × 1000 直接写进内部字段，两种来源必须得到相同的 LCOE。
    若相差 1000 倍，说明换算被遗漏或重复施加。"""
```

这条测试专防 Q1 指出的"相差千倍"错误，是 M1 的必过项。

**配置格式约定**：在 YAML 中显式标注单位，便于加载层校验。

```yaml
cost:
  unit_cost_energy:
    low: 0.35
    mid: 0.50
    high: 0.70
    unit: "元/kWh"        # 加载层按此字段决定换算因子
    internal_unit: "元/MWh"
```

对应地，`system.yaml` 的 `gpu_power_kw_per_card` 建议更名为 `gpu_power`，附 `unit: "kW/卡"`。

---

## U2 · YAML 语法修正（应 Q2）

已修正，共三处：

1. `chemistry_na.yaml` 的 `gamma`、`c_max_charge`、`c_max_discharge`、`eta_charge`、`eta_discharge`、`low_temp_charge_margin`
2. `chemistry_li.yaml` 同上六处
3. `system.yaml` 的 `pue`

修正方式：曲线统一为

```yaml
<curve_name>:
  points:
    - {T: -30, value: 0.80}
    ...
  source: "..."
  scale_range: [0.9, 1.0, 1.1]
```

`points` 与 `source`、`scale_range` 同层，`points` 的值是序列。原写法（序列后追加同层映射键）是非法 YAML。

**验收**：M1 必须实现一个测试，用 `yaml.safe_load` 解析 `configs/` 下全部 YAML，任一解析失败即报错。

```python
def test_all_configs_parse():
    for path in CONFIG_DIR.glob("**/*.yaml"):
        yaml.safe_load(path.read_text(encoding="utf-8"))
```

---

## U3 · 曲线类参数的区间（应 Q3）

SPEC §1.3 的 `{low, mid, high}` 要求原本只覆盖标量参数，曲线未覆盖。补充规则：

**曲线的敏感性扫描用整条曲线的乘性缩放**，字段为 `scale_range: [low, mid, high]`（mid 恒为 1.0）：

```
curve_scaled(T) = clamp( curve_base(T) × s , lower_bound, upper_bound )
```

clamp 规则按曲线类型：

| 曲线 | 下界 | 上界 | 说明 |
|---|---|---|---|
| `gamma` | 0.0 | 1.0 | 折减系数不能超过 1 |
| `c_max_charge` / `c_max_discharge` | 0.0 | +∞ | 倍率无上界约束 |
| `eta_charge` / `eta_discharge` | 0.0 | 1.0 | 效率不能超过 1 |
| `low_temp_charge_margin` | 0.0 | 1.0 | 安全裕度是保守系数 |

**为什么用缩放而不是准备三套曲线**：三套曲线意味着三倍的手工维护量，且三套之间的差异是人为设定的，缺乏依据。乘性缩放表达的是"参数不确定性区间"，与标量参数的 `{low, mid, high}` 语义一致（后者本质上也是缩放）。

**验收**：`test_chemistry_params.py` 中的 `test_gamma_within_unit_interval` 需在扫描 `scale_range` 的三个取值下都通过，且 `scale_range` 三个值产生的 LCOE 必须有显著差异（受 §9.11 约束，但限定在能激活该参数的样例上，见 U19）。

---

## U4 · 低温折减如何进入约束（应 Q4）

**这是本次复核中最重要的一条**：原 SPEC 未给出具体公式，而若按"收缩 SOC 上界"实现，会直接导致不可行。

### 问题复现

若采用 `soc_max(T) = 0.9 × gamma(T)`：

```
T = -25℃ 时，锂电 gamma ≈ 0.53 → soc_max = 0.9 × 0.53 = 0.477
而 soc_init = 0.50 > 0.477
→ 初始状态就违反上界 → MILP 直接 infeasible
```

这不是边界情形，而是北方冬季的**典型工况**，因此必须在规格层面定死。

### 采用的方案

**SOC 窗口固定不随温度收缩；温度折减通过功率与可用能量两条途径进入。**

#### (a) 功率上限（主要途径）

```
p_ch_max_x(t)  = min( P_nom_x , c_max_charge_x(T_amb(t)) × E_nom_x × gamma_x(T_amb(t)) )
p_dis_max_x(t) = min( P_nom_x , c_max_discharge_x(T_amb(t)) × E_nom_x × gamma_x(T_amb(t)) )
```

`c_max_charge` 已在 chemistry 模块内含 `low_temp_charge_margin`，调用方不要再乘一次。

#### (b) 可用能量上限（次要途径，防"空转高 SOC 却无能量可放"）

```
可放出能量上限：  e_dis_avail(t) ≤ E_nom_x × ( soc_x(t) − soc_min ) × gamma_x(T_amb(t))
可吸收能量上限：  e_ch_avail(t)  ≤ E_nom_x × ( soc_max − soc_x(t) ) × gamma_x(T_amb(t))
```

其中 `e_dis_avail(t) = p_dis_x(t) × Δt`、`e_ch_avail(t) = p_ch_x(t) × Δt`。

#### (c) SOC 动态仍按名义容量

```
soc_x(t+1) = soc_x(t) + [ eta_ch_x(T)·p_ch_x(t)·Δt − p_dis_x(t)·Δt / eta_dis_x(T) ] / E_nom_x
```

`E_nom` 是额定容量，不乘 gamma。gamma 只出现在 (a)(b) 两处。

### 为什么不选"收缩窗口"

除上面的不可行问题外，还有两点：

1. **语义混淆**。收缩上界等价于说"电池装不下这么多电"，而实际现象是"装得下但放不出来"（低温下可逆容量下降、极化增大）。用功率与可用能量限制更贴近物理。
2. **与 `soc_min` 的交互复杂**。上下界同时收缩会让可用区间被压缩两次，而缩多少没有依据。

### 初始 SOC 的取法

`soc_init = 0.50` 保持不变，因为窗口固定后它总是可行。但需注意：若某地区冬季长期低温，`gamma` 低会导致**实际可调度的能量远小于名义容量**——这正是本方案希望体现的机制，不需要通过初始条件额外补偿。

**验收**：新增测试

```python
def test_low_temp_charge_margin_binds():
    """在 T = -25℃ 下，充电功率上限必须显著低于 T = 25℃ 时的上限。
    且求解结果中，冬季时段的 p_ch 不应达到额定功率。"""
```

---

## U5 · 额定功率进入约束（应 Q5）

已含在 U4 的 (a) 式中：`p_ch_max = min(P_nom, c_max × E_nom × gamma)`。

**补充说明**：外层优化 P_nom 时，若 `c_max × E_nom` 始终远小于 P_nom，则功率投资变量对结果无影响（投资浪费），BO 会自然把它压到最小值。这是**正确行为**，不是 bug。

但需防止另一种情形：若 `P_nom` 从不成为紧约束，则 A1/A2/A3 三个臂的差异会主要来自能量容量，功率变量形同虚设。判断方法：检查最优解处的约束活性——

**验收**：在结果中记录每个时段的约束活性（功率约束是否取等、能量约束是否取等），并在报告中给出"功率约束活跃时段占比"。若该比例接近 0 且 P_nom 优化结果贴下界，说明功率维度在本场景下不重要，应在报告中说明，而不是掩盖。

---

## U6 · 循环、衰减与替换成本口径（应 Q6）

原 SPEC 写"Σ(p_dis·Δt/E_nom)/2"，与"吞吐量/容量/2"不等价，且未说明口径。统一如下。

### 等效循环次数（唯一口径）

```
等效循环次数 =  ( Σ_t p_dis_x(t) · Δt ) / E_nom_x
```

- **放电量在电池侧计量**（即已扣充电效率影响之后、放电效率之前的内部能量），与 SOC 动态一致。
- 物理含义：满充满放一次 = 1 次循环。1 MWh 电池放出 1 MWh 记 1 次。
- **不使用 /2**。原文的 /2 是把"半循环计一次"与"吞吐量"两种口径混在了一起。
- 该口径固定写死在代码中，不提供切换开关（切换会让结果不可比）。

### 容量衰减

```
cycle_fade    = min(1.0, 等效循环次数 / cycle_life)
calendar_fade = min(1.0, elapsed_years / calendar_life)
total_fade    = min(1.0, cycle_fade + calendar_fade)
SOH           = 1 − total_fade
```

- **线性相加**，不用平方和。理由：两者是独立老化机制，线性相加更保守，且不引入额外假设。
- `Elapsed_years` 为显式输入的耗时（见 U18 的接口修正）。
- 衰减仅用于**横向比较不同策略对寿命的影响**，不作为精确老化模型。报告中必须标注为估算（SPEC §10.1）。

### 替换成本

替换成本按"电池寿命耗尽后需再投入一次建设成本"折算到年：

```
replacement_cost_annual = E_nom · unit_cost_energy_internal · max(0, 1 / battery_life_years − 1 / project_life_years)
其中 battery_life_years = min( calendar_life , cycle_life / 年等效循环次数 )
```

- `max(0, ...)` 处理"电池寿命长于项目周期"的情形（此时不产生替换成本）。
- **避免重复计算**：若 `battery_life_years` 已由循环寿命决定，则衰减本身不再单独计价；衰减只用于报告 SOH，不进入成本。成本侧只看寿命是否短于项目周期。

### CRF 用哪个寿命

统一用**项目寿命** `project_life_years`（economics.yaml 中为 20 年）：储能资产的资本回收按项目周期摊销，替换成本单独按 U6 的公式计入。

`finance.project_life_years` 与化学文件的 `lifetime.calendar_life` 是两个不同的量，不要混用。

---

## U7 · S2 硬窗口与 S3 软窗口（应 Q7）

原文 §3.1 允许 S3 延迟，§3.3 却把所有窗口截到 `deadline`，无法产生合法逾期。**重新定义任务的三个时间参数**：

| 参数 | S1 | S2 | S3 |
|---|---|---|---|
| `arrive_h` | — | ✓ | ✓ |
| `deadline_h` | — | ✓ 硬截止 | ✓ 期望完成时刻 |
| `tolerance_h` | — | 0 | config 中按画像给出（建议 12–48 h） |
| 最晚完成时刻 `latest_h` | — | `deadline_h` | `deadline_h + tolerance_h` |

**启动窗口**：

```
S2:  start_i(t) 仅在 t ∈ [arrive_i, deadline_i − dur_i] 可取 1，且 Σ start_i = 1
S3:  start_i(t) 仅在 t ∈ [arrive_i, latest_i − dur_i] 可取 1
     若任务在 deadline_i 之后才启动 → 计入延迟（用于 SLA 惩罚）
     若到 latest_i 仍未执行 → 该窗口不可行（生成器需保证 latest_i 落在窗口内）
```

**截止时刻的边界含义**：统一采用**左闭右开**——任务在 `deadline_h` 时刻完成（即最后一个运行时段为 `deadline_h − 1`）视为按时。该约定必须在报告中写明，并体现在测试中。

**对生成器的要求**：`generate_tasks` 必须保证所有任务的 `latest_h ≤ horizon_hours`，否则该任务在窗口内不可能完成，会导致不可行。生成后需校验并告警。

**验收**：

```python
def test_s3_can_finish_after_deadline():
    """构造一个必然需要延迟的 S3 任务（截止前后负荷饱和），
    断言求解成功且该任务被标记为延迟，而不是 infeasible。"""
```

---

## U8 · SLA 惩罚的计法（应 Q8）

原文把整周延迟比例写在 `Σ_t` 内，按字面会在 168 小时的求和里被累计 168 次。**修正如下**。

### 内层目标函数（完整版，替换 SPEC §3.4）

```
min  Σ_t [ g_imp(t)·Δt·price_buy(t)
         − g_exp(t)·Δt·price_sell(t)
         − Q_ind(t)·price_ind_heat·Δt
         + P_curtail(t)·pen_curtail·Δt ]
     + SLA_penalty_week
```

其中

```
SLA_penalty_week = ( 延迟任务数 / 总任务数 ) · pen_sla · MTok_week
```

- `SLA_penalty_week` **在 Σ_t 之外**，整个窗口只计一次。
- `MTok_week` 为该窗口的总 Token 产量（百万 Token）。
- `延迟任务数` = 完成时刻晚于 `deadline_i` 的任务数（S3 适用；S2 因硬窗口定义上不会延迟，但若允许 S2 违约则同样计入）。

### 为什么这个修正不影响最优解，但仍必须做

由于 `SLA_penalty_week` 对给定任务集是**常数**（任务数固定），它不改变同一配置下不同调度方案之间的排序——因此不影响 argmin。但它影响：

1. **跨配置比较**。不同配置可能完成不同数量的任务（若允许放弃），此时该常数不同。
2. **报告口径**。成本数字若包含被累计 168 次的惩罚项，会完全失真。

因此必须修正，且需在报告中说清楚：**内层目标值 ≠ 实际经济成本**。报告中的"运营成本"指标应使用 Σ_t 部分（不含 SLA 惩罚），SLA 单独作为质量指标报告，这也对应外层 f4 与 f1 的分工（见 U11）。

### SLA 达标率的定义

```
SLA_达标率 = 按时完成任务数 / 总任务数
按时 = 完成时刻 ≤ deadline_h（左闭右开，见 U7）
```

**分母是否包含 S1**：S1 已并入 `IT_base` 时间序列（见 U10），不进入任务表，因此**分母只含 S2 与 S3**。这一点必须在报告中写明，否则"98% 的 SLA 达标率"含义模糊。

**内部比例与报告百分数的转换**：内部用 [0, 1] 的比例，报告中乘 100 并保留两位小数。外层 f4 = 1 − SLA_达标率，也是 [0, 1]。

---

## U9 · 五个策略臂的实现（应 Q9）

原文用权重组合无法表达"碳排优先""FCFS"这些语义。**改为每个策略有独立的标量目标**，权重仅用于展示与权衡分析。

| 策略 ID | 内层目标（最小化） | 说明 |
|---|---|---|
| `S-cost` | Σ_t [购电成本 − 售电收入 − 工业供热收入 + 弃电惩罚] | 纯经济口径 |
| `S-carbon` | Σ_t [g_imp·Δt·CI_grid − Q_avoid·CI_substituted] | 毛碳排减供热替代，单位 kgCO₂，**量纲不同，需归一化比较** |
| `S-heat` | −Σ_t Q_ind(t)·Δt + pen_mun_short · 市政供热缺口 | 最大化工业供热量，市政缺口重罚 |
| `S-balance` | 上述三项各自归一化后加权和 | 权重来自 experiments.yaml，需公开 |
| `S-base` | 固定规则，不做优化 | 见下 |

### 关于 `S-carbon` 的量纲问题

碳排量级（kgCO₂）与经济量级（元）不可直接相加。`S-carbon` 是**单目标优化**（只最小化碳），不与其他项混合，因此不存在量纲问题。`S-balance` 需要加权时，三项各自按试点实验确定的 min-max 范围归一化到 [0, 1]（归一化范围必须固定并记录，见 U17）。

### 关于 `S-heat` 在无供热地区的退化

R-SOUTH 的市政供热需求为 0（见 U12 的修正）。此时 `S-heat` 的市政缺口项恒为 0，目标退化为"最大化工业供热"——但若该地区工业供热能力也为 0（U12 的修正），则目标退化为常数，优化退化为随机可行解。

**处置**：规格明确允许 `S-heat` 在无供热画像下退化为 `S-cost`，并在结果表中标注 `strategy_collapsed: true`。**不要**为了让它在南方也"有意义"而人为设定工业供热需求。

### `S-base` 的固定规则（不可用权重表达的部分）

```
任务调度：FCFS —— 按 arrive_h 升序，依次放入最早可用的可行时段
储能：不主动套利 —— p_ch = p_dis = 0，除非出现功率盈余/缺额必须由储能平衡
供热：热泵按可回收余热上限满出力，优先满足市政刚性需求，剩余给工业
购电：先消纳可再生，缺口全由电网补充
```

`S-base` 不需要求解器（或等价于求解一个无自由度的 LP），它的作用是给出方法对照基线。

### 策略评分与实际经济成本分离

**结果表必须同时报告两个量**：

- `objective_value`：该策略实际优化的目标值（量纲随策略变化）
- `operating_cost_yuan`：统一口径的实际运营成本（Σ_t 部分，不含 SLA 惩罚）

**禁止**用不同策略的 `objective_value` 直接比较优劣——它们不是同一个量。跨策略比较只看 `operating_cost_yuan` 与各项质量指标。这一点需在报告中显式声明。

---

## U10 · S1 与 GPU / Token 核算（应 Q10） 必读

### GPU 容量的拆分管线

```
gpu_capacity_total   = 4096          # 集群总卡数（system.yaml）
gpu_capacity_s1      = 由 IT_base 时间序列隐含（见下）
gpu_capacity_plan    = gpu_capacity_total − gpu_capacity_s1
```

- **S1 不进入整数规划**，因此不占用规划变量。
- `IT_base` 时间序列**已包含** S1 的 GPU 负荷与所有不可迁移负荷。这是 S1 并入基础负荷的完整含义。
- 约束 `Σ_i gpu_i · y_i(t) ≤ gpu_capacity_plan` 中的上限必须用**扣掉 S1 之后**的容量，而不是 `gpu_capacity_total`。

**潜在冲突**：若 `IT_base` 对应用户设定的 8 MW 基础负荷，而 S1 任务生成时又单独占用了 GPU 卡数，会重复计算。

**处置**：规格明确 —— `IT_base` 是**输入序列，不再由代码反推 S1 的 GPU 占用**。二者关系由 config 显式给出：

```yaml
compute:
  gpu_capacity_total: 4096
  # S1 隐含占用的卡数（用于计算规划可用容量）；由 config 给出，不由代码反推
  gpu_capacity_s1_reserved: 1024
  gpu_capacity_plan: 3072          # 或由 total − reserved 自动计算
  kpi_base_mw: 8.0
  it_base_includes_s1: true        # 显式声明，防止重复计算
```

### Token 产量核算

```
总 Token (MTok) = 规划任务 Token + S1 Token
规划任务 Token = Σ_i y_i(t) · gpu_i · token_per_gpu_hour · Δt    （对 i, t 求和）
S1 Token     = IT_base 对应的 GPU·h × token_per_gpu_hour × Δt
```

- 需要 config 提供 **S1 对应的等效 GPU·h 序列**（或一个由 `IT_base` 换算的系数）。
- 若无法给出，则**报告口径收窄为"仅统计规划任务的 Token"**，并在报告中明确说明，避免漏算被误认为总量。

### 验收

```python
def test_s1_not_double_counted():
    """校验：规划任务的 GPU 占用峰值 ≤ gpu_capacity_plan；
    Token 总量 = 规划任务 Token + S1 Token，且二者均非负。"""
```

---

## U11 · LCOE 的分母与 A0 的处理（应 Q11）

### 问题

原 SPEC §4.3 用"年放电量"作分母。A0 臂无储能，分母为 0 → LCOE = ∞，跨臂不可比。`objective.py` 又用了 `annual_energy_served_mwh`（"实际服务的电量"），与原文不一致。

### 采用的方案

**分母统一为「年服务电量」**，定义为：

```
annual_energy_served_mwh = Σ_t IT_load(t) · Δt   （年化后）
```

即**AIDC 实际消耗的 IT 电量**，含基础负荷与全部任务负荷。这个量在四个臂中都非零且可比，物理含义清晰（"每服务一单位算力用电的全成本"）。

**不采用"储能放电量"**：那个量对 A0 为零，且不能反映算力服务本身。

### 分子口径

```
LCOE = ( annual_operating_cost + storage_annualized_cost ) / annual_energy_served_mwh

annual_operating_cost = Σ_t [ 购电成本 − 售电收入 − 工业供热收入 + 弃电惩罚 ]   （年化）
storage_annualized_cost = CRF · (E·unit_cost_E + P·unit_cost_P) + E·opex_annual + replacement_cost_annual
```

**明确排除 SLA 惩罚项**：SLA 是服务质量指标，不是能源成本。它单独作为 f4 报告。

**明确包含哪些收入**：售电收入与工业供热收入计入（它们抵减成本）。**市政供热的内部结算价不计入收入**——除非 config 显式声明它是对外结算（见 U12）。

### 跨臂比较的正确方式

LCOE 是可比的，但**不宜作为唯一比较指标**，因为不同臂的配置成本差异很大。结果表必须同时给出：

- `LCOE`（元/MWh）
- `net_annual_cost` = `annual_operating_cost + storage_annualized_cost`（元/年）
- `total_annualized_cost_per_mtok`（元/MTok）

三个指标一起看，才能判断"储能带来的成本增加是否被收益抵销"。

**A0 的处理**：正常计算即可。`storage_annualized_cost = 0`，分母非零，LCOE 有定义。

---

## U12 · 供热的需求上下界（应 Q12） 

原 SPEC 只给市政需求下界，没有上界，会导致"超需求供热"被计为替代减排（虚增碳收益）。

### 修正为上下界

```
市政供热：  Q_mun_demand(t) ≤ Q_mun(t) ≤ Q_mun_demand(t) · (1 + heat_tolerance)
工业供热：  0 ≤ Q_ind(t) ≤ Q_ind_capacity(t)
```

- `heat_tolerance`：允许的小幅超额（默认 0.05），用于避免求解器在刚性需求上出现数值困难。
- 超额部分**不计入碳抵扣**（只有实际被接纳的热量才算替代）。
- `Q_ind_capacity` 为该地区的工业用热接纳能力，由 config 给出。**若无工业用热需求，必须设为 0**，而不是保留一个售价让求解器凭空产生收入。

### R-SOUTH 的修正

```yaml
heat:
  q_mun_demand_peak_mw_th: 0.0
  q_mun_demand_base_mw_th: 0.0
  q_ind_capacity_mw_th: 0.0       # 新增：无工业用热需求
  heating_season_months: []
```

原文件只把市政置零而保留工业售价，这会让求解器在南方也产生"供热收入"。修正后，R-SOUTH 的余热回收确实不具备经济价值——这是真实的对照条件，也是该画像预期给出"锂电为主"结论的原因之一。

### 市政结算价的性质

`heat_price_mun` 的语义必须明确：它是**内部结算价**（用于内部核算），还是**对外结算价**（真实收入）？

**默认按内部结算处理，不计入 LCOE 的收入项**。若某地区确有对外供热合同，需在 config 中显式声明：

```yaml
heat:
  heat_price_mun: 50.0
  heat_price_mun_is_external: false    # false = 内部结算，不计入收入
```

这一点直接影响 LCOE，必须在报告中写明所采用的处置。

---

## U13 · 碳公式修正（应 Q13）

### 修正后的碳核算（替换 SPEC §2.5）

```
carbon_grass(t)   = g_imp(t) · Δt · CI_grid(t)                    [kgCO₂]
carbon_avoid(t)   = Q_mun_accepted(t) · Δt · CI_substituted        [kgCO₂]
                   其中 CI_substituted 单位 kgCO₂/MWh_th，Q_mun_accepted 为实际接纳热量
carbon_net(t)     = carbon_grass(t) − carbon_avoid(t)             [kgCO₂]
carbon_per_mtok   = Σ_t carbon_net(t) / tokens_mtok
```

**修正点**：

1. 原式漏乘 Δt。固定 1 h 时数值碰巧正确，但单位不闭合。已补。
2. 原式 `/1000×1000` 相互抵消，无意义。已删。
3. 碳抵扣用**实际接纳热量** `Q_mun_accepted`（受 U12 上界约束），而非 `Q_mun`（可能超需求）。
4. `CI_substituted` 的单位明确为 **kgCO₂/MWh_th**，`CI_grid` 为 **kgCO₂/MWh**。两者不可互换。

### 新增：制造阶段碳是否计入净碳指标

SPEC 要求有这个开关，YAML 中缺失。补充到 `economics.yaml`：

```yaml
carbon:
  include_manufacturing_in_net: false    # 是否把储能制造阶段碳足迹计入净碳指标
  # false：净碳只算运行阶段（购电碳排 − 供热替代）
  # true ：再加上储能制造碳足迹的年化摊销（见 carbon.py::lifecycle_carbon_storage）
  # ⚠️ 无论取何值，报告中必须写明当前口径。切换口径会使不同报告的数字不可比。
```

**建议默认 false**：制造阶段碳足迹的数据质量较差（口径差异大），混入运行碳排会引入额外不确定性。若单独报告，作为"储能隐含碳"列在附录中。

---

## U14 · 正常调度与故障调度分离（应 Q14） 

**这是原 SPEC 的结构性缺陷**：故障评估若强制满足全部需求与初末 SOC 相等，只会得到 infeasible，无法计算 EENS。

### 两套模式，两套约束

| 约束 | `mode="normal"` | `mode="fault"` |
|---|---|---|
| 功率平衡 | 硬等式 | 硬等式 + **失供变量** `unserved_elec(t) ≥ 0` |
| IT 负荷满足 | 必须全部满足 | 允许失供，`unserved_elec` 计入 EENS |
| 任务完成 | 全部任务必须完成 | 可迁移任务全部暂停；关键负荷必须满足 |
| SOC 终端约束 | `soc(0) == soc(T_end)` | **取消**（放电到下限是允许的） |
| 市政供热 | 刚性需求必须满足 | 允许缺口，`unserved_heat` 计入热中断损失 |
| 目标函数 | 按策略的经济/碳目标 | **不用经济目标**，只最大化关键负荷保障 |
| 充放策略 | 优化决定 | 降级规则（U15） |

### 失供变量的引入

在 `mode="fault"` 下，功率平衡改为：

```
P_pv_used(t) + P_wind_used(t) + g_imp(t) + p_dis_Na(t) + p_dis_Li(t) + unserved_elec(t)
  = critical_load(t) + p_ch_Na(t) + p_ch_Li(t) + P_hp(t)
```

其中 `critical_load` 见 U15。`unserved_elec(t)` 是决策变量，物理含义为"未能供上的关键负荷"。

### §9 不变量的适用范围（必须明确）

原 SPEC 写"每组结果都通过"，但故障轨迹不满足终末 SOC 相等等条件。**修正为**：

| 不变量 | 正常轨迹 | 故障轨迹 |
|---|---|---|
| §9.1 功率平衡 | ✓ 通过（含 unserved 项） | ✓ 通过（含 unserved 项） |
| §9.2 SOC 越界 + 首末相等 | 首末相等 ✓ | **仅越界检查，不做首末相等** |
| §9.3 充放互斥 | ✓ | ✓ |
| §9.4 功率上限 | ✓ | ✓ |
| §9.5 任务完整性 | ✓ | **不适用**（任务暂停） |
| §9.6 集群容量 | ✓ | **不适用** |
| §9.7 市政供热刚性 | ✓ | **改为检查 `unserved_heat` 已记录** |
| §9.8 热泵上限 | ✓ | ✓ |
| §9.9–9.12 | ✓ | ✓ |

测试需按轨迹类型参数化，不能一套断言打天下。

---

## U15 · 韧性评估的完整参数（应 Q15） 

需要补充的 config 参数（写入 `economics.yaml` 与各地区的 `regions/*.yaml`）：

```yaml
resilience:
  grid_outage_hours: [2, 6, 12]
  n_samples_per_outage: 5
  renewable_drop_percentile: 10
  renewable_drop_hours: [6, 12]
  n1_storage_failure: true

  # ---- 以下为新增 ----
  # 关键负荷拆分（占 IT_base 的比例）
  critical_load_fraction:
    online_inference: 0.60      # 在线推理（不可迁移）
    base_cooling: 0.25          # 基础制冷
    safety_system: 0.15         # 安全系统
    # 三者之和必须为 1.0，且代表 IT_base 中被保障的部分
    # 校验：critical_load(t) = IT_base(t)  （即故障时保障全部不可迁移负荷）
    # 若业务上只保障其中一部分，改这里的比例，但需在报告中说明含义差异

  # 故障初始 SOC
  fault_initial_soc: 0.50        # 或 "from_normal_end"：取故障前正常调度结束时的 SOC
  # ⚠️ 两种取法含义不同，必须固定其一并写明：
  #   固定值  → 评估"任意时刻突发故障"的平均情况
  #   from_normal_end → 评估"正常调度刚结束就故障"的特定情形
  #   推荐 "from_normal_end"，并与正常轨迹衔接（更贴近实际）

  # 储能放电顺序（降级策略的规则）
  discharge_order: [Na, Li]      # 按此顺序放电，先放钠电
  parallel_discharge: false      # false = 顺序放电；true = 按功率比例同时放电
  # ⚠️ 顺序放电的理由是保护循环寿命更长的体系（此处设定为钠电），
  #    该选择依赖参数标定结果，标定后需复核是否仍合理

  # 电与热的优先级
  priority: "electricity_first"  # 电优先；热在电满足后由剩余余热提供

  # 热中断损失
  heat_outage_cost_per_mwh: 500.0   # 元/MWh_th，用于把热失供折算进 gap_cost

  # N-1 通过阈值
  n1_pass_threshold_hours: 6.0      # T_island ≥ 6h 视为通过
  # ⚠️ 该阈值直接决定 n1_pass_rate 指标，必须报告，且不应事后调整
```

### f3 的完整定义（替换 SPEC §5.3）

```
gap_cost = EENS · gap_cost_per_mwh + heat_unserved · heat_outage_cost_per_mwh

f3 = gap_cost / baseline_gap_cost        （归一化，见 U17）
```

原 SPEC 提到"孤岛指标参与 f3"但公式里没有对应项，现补齐：

```
f3 = w_eens · (EENS / EENS_ref) + w_island · max(0, 1 − T_island / T_island_ref)
```

其中 `EENS_ref`、`T_island_ref` 为归一化参考值（取基线臂 A0 的结果），权重 `w_eens`、`w_island` 在 config 中给出（建议 0.5 / 0.5）。**参考值与权重必须固定并报告**，否则 f3 不可复现。

### 低保真如何获得 f3

低保真评估（12 个代表日）**无法覆盖故障场景**（故障场景需要连续多小时轨迹）。处置：

- **低保真评估时 f3 用简化代理**：取代表日中最低温日的可用能量裕度作为代理指标（低温可用容量越少，韧性越差）。
- 或：低保真只优化 f1/f2/f4，f3 在高保真确认时才计算，BO 用 3 目标。

**推荐后者**（更干净）。规格明确：**低保真阶段 f3 = NaN，BO 在三维目标上搜索；高保真确认阶段补齐 f3 并重新过滤 Pareto 前沿**。实现时需处理 NaN 在 GP 训练中的传播（用 mask 或分组）。

---

## U16 · T_island 的观察边界（应 Q16） 

### 定义（修订 SPEC §5.3）

```
T_island = 从故障开始时刻起，关键负荷被 100% 满足的连续时长，
           直到首次出现 unserved_elec(t) > 0 为止
```

### 右删失处理（关键）

原 SPEC 未处理"场景结束时仍能供电"的情形。G2 场景（2h 失电）若两小时内一直正常供电，得到的是**删失观测**"T_island ≥ 2h"，不能直接记为 2h——否则会系统性低估长续航配置的韧性。

**处置**：

1. `ResilienceResult` 增加字段 `censored: bool`。
2. 若 `unserved_elec` 在场景窗口内始终为 0，则 `t_island_h = duration_h` 且 `censored = True`。
3. 聚合时：
   - `t_island_mean` 计算为**下界均值**（删失观测按窗口长度计入），并在报告中注明这是下界。
   - `t_island_worst` 取所有场景中的最小值（删失场景按窗口长度）。
   - 另需报告**删失比例**，若过高（如 > 50%）说明场景时长不足以区分配置，需延长故障时长。

4. **推荐增加一个长时场景**（如 G24，失电 24h），专门用于区分续航能力。否则 G2/G6 场景对多数配置都会删失。

### R-drop 与 N-1 如何产生孤岛指标

这两类场景**本身不切断电网**，因此"孤岛续航"对它们没有直接定义。处置：

- 对 R-drop 与 N-1，**不报告 T_island**，而是报告在该场景下关键负荷是否被完整满足（布尔）与 EENS。
- 若需要 T_island，需组合场景：**R-drop + 同时失电**（这是实际中最危险的工况：风光骤降叠加电网故障）。规格要求实现这个组合场景，标记为 `R-drop+G`。

**验收**：场景清单表（`scenarios_summary`）中需列出全部场景及其是否报告 T_island。

---

## U17 · 多保真、地区生成与 BO 对照的口径（应 Q17）

### 代表日之间是否连接 SOC

**不连接。每个代表日独立求解，SOC 初始化为 `soc_init`，终端约束也只要求单日内首末相等。**

理由：代表日是"典型工况抽样"，彼此不构成连续时间序列。若强行连接，会把不同季节的 SOC 状态串起来，产生无物理意义的约束。

**代价**：会忽略跨日储能调度（如连续多日大风的作用）。**处置**：高保真（全年连续）用于确认；若低保真与高保真结论不一致，以高保真为准，并在报告中说明这一差异——**这本身是一个值得报告的发现**。

### 任务如何映射到代表日

**同一份任务表在 12 个代表日各跑一次**（任务按 `arrive_h` 模 24 映射到代表日内的时段）。这样保证四个配置臂面对完全相同的任务序列，可比性优先。

### 四季周轨迹如何年化

```
年化值 = Σ_{d ∈ 代表日} ( 该日结果 × weight_d × 365 )
```

其中 `weight_d` 为该代表日代表的年天数占比（`select_representative_days` 返回，和为 1）。

⚠️ **权重必须在结果中记录并输出**，否则年化值不可复现。

### 滚动窗口如何承接状态

高保真全年求解（若规模不允许）采用 24h 滚动窗口逐日推进：

- SOC 在窗口之间**连续传递**（前一日末状态 = 后一日初状态）。
- 只有第一个窗口要求 `soc(0) == soc_init`；最后一个窗口要求 `soc(T_end) == soc_init`（闭环）。
- 任务若跨窗口，需在进入窗口时就已确定启动时刻（不允许跨窗口重新决策）。**建议生成器把任务时长限制在 ≤ 24h**，简化此问题。
- 储能状态传递时需注意：不可行窗口会导致后续全部不可行，因此每个窗口的可行性需单独检查并记录。

### 室内储能的温度

原 SPEC 未定义。补充：

```yaml
storage_site:
  outdoor_installation: true      # true = 储能温度 = 环境温度
  indoor_temp_offset: 15.0        # 室内布置时，储能温度 = 环境温度 + 该偏移（℃）
  indoor_temp_floor: 15.0         # 室内温度下限（℃，受机房影响）
  indoor_temp_ceiling: 35.0       # 室内温度上限
```

- `outdoor_installation: true` → `T_storage(t) = T_amb(t)`（这是低温折减机制真正起作用的场景）
- `outdoor_installation: false` → `T_storage(t) = clip(T_amb(t) + offset, floor, ceiling)`

**R-EAST 与 R-SOUTH 设 false**，这直接决定了它们的低温折减几乎不起作用，是预期给出"锂电为主"结论的机制之一。

### BO 与网格的预算匹配

| 项 | 设置 |
|---|---|
| BO 低保真预算 | 200 次 |
| 网格扫描 | 每维 6 点起步（6⁴ = 1296），跑通后补到 10 点（10⁴） |
| 对比方式 | **按评估次数对齐**：取网格前 N 个点（N = 200）计算 hypervolume，与 BO 的 200 次曲线比 |
| 权重向量 | 20 组随机单纯形采样，BO 与网格用**同一组权重** |
| 高保真确认 | 取各自 Top-5 候选，用高保真评估，比较最终指标 |

### 归一化范围与超体积参考点

```
归一化：各目标按试点实验（如 50 次随机采样）的 min-max 范围线性映射到 [0, 1]
        范围固定后写入 BOResult.normalization，后续所有实验复用
超体积参考点：归一化后统一取 [1.1, 1.1, 1.1, 1.1]
不可行点：直接跳过，不进入 GP 训练，不参与 hypervolume 计算
```

**不可行点的处理需记录**：若某区域大量不可行（如储能容量过小导致 SLA 无法满足），BO 需要约束建模（feasibility-weighted acquisition），而不是简单地丢掉。规格要求：**记录不可行率，若 > 30% 需在报告中说明并考虑调整搜索空间**。

### 任务表在不同保真度下必须一致

低保真与高保真**使用同一份任务生成参数与种子**，任务数量按窗口长度等比例缩放。这样"低保真筛选出的配置在高保真下也应表现良好"，否则多保真链路失效。

---

## U18 · 接口与 milestone 依赖修正（应 Q18）

### SOC 数组长度

`storage.py::soc_series` 的 docstring 有歧义。**明确约定**：

```python
def soc_series(p_ch, p_dis, T_amb, spec, dt=1.0, soc_init=None) -> np.ndarray:
    """返回长度为 T+1 的数组。
    索引 0 为初始 SOC，索引 k 为第 k 个时段结束时的 SOC。
    即 soc[0] = soc_init，soc[t+1] 由 p_ch[t], p_dis[t] 递推得到。
    """
```

**验收**：`len(soc) == len(p_ch) + 1`。

### 衰减函数的时间输入

```python
def capacity_fade(
    cycles: float,
    elapsed_years: float,        # 新增：显式输入耗时（年）
    spec: StorageSpec,
    model: str = "cycle_plus_calendar",
) -> float:
    """见 U6 的公式。"""
```

移除 `throughput_mwh` 参数（等效循环已包含吞吐量信息，重复输入易导致口径混乱）。

### milestone 依赖顺序修正

原顺序中 M3 需要四个目标，但韧性模块排在 M6；M4 需要优化配置，但 M5 尚未完成。**修正为**：

| 阶段 | 内容 | 依赖 |
|---|---|---|
| M1 | 数据与参数层（含韧性参数字段，但只有占位） | — |
| M2 | 内层 MILP（含 `mode="normal"` 与 `mode="fault"` 两套约束） | M1 |
| M2.5 | 韧性评估（提前到此，供 M3 使用 f3） | M2 |
| M3 | 外层目标评估（f1–f4 全部可用） | M1, M2, M2.5 |
| M4 | 4 臂 × 5 策略 × 5 种子（用 config 中的固定候选配置） | M3 |
| M5 | 外层 BO 与网格扫描对照 | M4 |
| M6 | 三组敏感性分析 | M5 |
| M7 | 地区映射 | M6 |
| M8 | 报告与图表 | M7 |

### A1/A2/A3 的固定候选配置

原 experiments.yaml 只给 A0 的固定值。**补充**（M4 阶段使用，M5 之后替换为优化结果）：

```yaml
arms:
  - id: A0
    sizing_fixed: {E_Na: 0.0, P_Na: 0.0, E_Li: 0.0, P_Li: 0.0}
  - id: A1
    sizing_fixed: {E_Na: 0.0, P_Na: 0.0, E_Li: 100.0, P_Li: 25.0}   # M4 用
    sizing_free: [E_Li, P_Li]                                        # M5 用
  - id: A2
    sizing_fixed: {E_Na: 100.0, P_Na: 25.0, E_Li: 0.0, P_Li: 0.0}
    sizing_free: [E_Na, P_Na]
  - id: A3
    sizing_fixed: {E_Na: 100.0, P_Na: 20.0, E_Li: 50.0, P_Li: 20.0}
    sizing_free: [E_Na, P_Na, E_Li, P_Li]
```

**重要**：M4 阶段四臂的**总容量或总投资应当可比**，否则"配置优劣"会混入"投资规模差异"。补充一个约束：

```
M4 阶段：四臂的总能量容量之和固定为 150 MWh，总功率之和固定为 45 MW
         （A0 例外，为 0，用于建立基线）
或在报告中对 A1/A2/A3 按投资额归一化比较
```

**规格要求**：结果表必须给出每臂的投资额，且跨臂比较时说明是否已做规模归一化。**这一点在核心演示图中尤其重要**——若 A3 只是因为投资更多而表现更好，那不能归因于低温机制。

---

## U19 · 验收条件不预设结果（应 Q19）

**这是立场问题，不是技术歧义。** 原 SPEC §11 与 §12 中以下表述必须删除或改写：

| 原文 | 问题 | 改为 |
|---|---|---|
| "至少识别出一个结论翻转的参数区间" | 预设结果 | "若未识别出翻转区间，须分析原因（参数是否激活、区间是否过窄）并说明" |
| "推荐表中至少一个画像给出锂电为主或混合" | 预设结果 | "推荐表须给出真实结论；若四画像结论一致，须给出归因分析（气候/布置/供热条件是否真正生效）" |
| "分层配置同时改善三项" | 预设结果 | 删除，改为"如实报告各臂在三项指标上的表现" |

### §9.11 参数生效测试的限定

原文"改参数必须改变结果"过强。**修正为**：

```
测试限定在能激活该参数的样例上：
- A0 臂下储能参数不变是合法的（无储能，参数无作用域）
- 某约束未绑定（如面积约束始终不紧）时，相关参数不变也是合法的

正确做法：检查该参数在模型中的系数矩阵是否非零，
          以及在可行解附近是否可能成为紧约束。
若系数为零 → 配置未被读取，是 bug。
若系数非零但结果不变 → 可能是约束不紧，需在报告中说明。
```

### 核心演示图的归因要求

原 §12 要求"只换储能配置，变量只有一个"。**补充严格要求**：

- 若四臂的投资额不同，则"变量只有一个"的说法不成立——实际变化的是"配置 + 投资额"两个量。
- **处置**：核心演示图必须同时给出**等投资额**的对照（按单位投资额归一化，或选一组投资额相近的配置），或者在图注中明确说明投资额差异。
- 否则"低温机制导致改善"的结论会被"投入更多所以更好"替代解释。这是最容易被打的一点。

---

## U20 · 严格确定性的条件（应 Q20）

仅固定随机种子不足以规定确定性。**补充要求**：

### 求解器设置（必须在 config 中固定）

```yaml
solver:
  name: "highs"
  threads: 1                    # 单线程，消除并行求解的浮点不确定性
  random_seed: 0                # 求解器自身的种子（HiGHS: random_seed）
  mip_gap: 0.01
  time_limit_s: 60.0
  presolve: true
```

### 多等价最优解的处理

MILP 存在多个等价最优解时，不同求解器/版本/线程数可能返回不同解，**这不是 bug**。§9.9 的确定性测试需要规定比较规则：

```
若两次运行的目标值差异 < 1e-9，则判定为"同一最优值"：
  - 目标值必须一致（容差 1e-9）
  - 轨迹允许不同，但必须各自通过全部不变量检查
  - 结果表中记录等价解的数目（若可获得）
若目标值差异 ≥ 1e-9 → 判定为不确定，需排查
```

### 浮点比较容差

```
目标值：1e-9（相对）
轨迹逐元素：1e-12（绝对，单位 MW/MWh）
SOC：1e-9
```

### 随机数使用规范

- 禁止全局 `np.random`（已在 §1.2 规定）
- 每个模块的函数若含随机性，`seed` 必须是**显式参数**，不得从环境或全局读取
- 并行执行时，每个 (arm, strategy, seed, season) 组合的种子必须由其索引确定性生成，例如：
  `seed_combo = base_seed + hash((arm, strategy, seed, season)) % 10**6`
  且哈希需用稳定实现（不要用 Python 内置 `hash`，它对字符串有随机化）

---

## 附：需要同步修改的既有文件

| 文件 | 修改点 |
|---|---|
| `SPEC.md` §1.1 | 替换单位禁令为 U1 的单位表与换算表 |
| `SPEC.md` §2.3 | 替换为 U4 的低温折减三式；SOC 窗口不再随温度收缩 |
| `SPEC.md` §2.4 | 补供热上下界（U12） |
| `SPEC.md` §2.5 | 替换为 U13 的碳公式，补 Δt |
| `SPEC.md` §3.1–3.4 | 按 U7、U8、U9 重写任务窗口、目标函数、策略定义 |
| `SPEC.md` §4.3 | 分母改为年服务电量（U11），补三指标并列报告 |
| `SPEC.md` §5.3–5.4 | 按 U15、U16 补齐参数与删失处理 |
| `SPEC.md` §9 | 补轨迹类型适用范围表（U14）与确定性规则（U20） |
| `SPEC.md` §11 | 按 U18 重排 milestone，补固定候选配置 |
| `SPEC.md` §11、§12 | 按 U19 删除预设结果的验收条件 |
| `configs/chemistry_*.yaml` | 已修（U2、U3） |
| `configs/system.yaml` | 已修 PUE（U2）；补 GPU 拆分字段（U10） |
| `configs/economics.yaml` | 补韧性参数（U15）、制造碳开关（U13） |
| `configs/regions/r_south.yaml` | 补 `q_ind_capacity_mw_th: 0`（U12） |
| `configs/regions/*.yaml` | 补 `indoor_temp_offset` 等字段（U17） |
| `configs/experiments.yaml` | 补 A1/A2/A3 固定配置（U18） |
| `src/aidc/model/storage.py` | 修正 `soc_series` 长度约定与 `capacity_fade` 签名（U18） |
| `src/aidc/model/dispatch.py` | 补 `mode` 参数与两套约束（U14） |
| `src/aidc/resilience/metrics.py` | 补 `censored` 字段（U16） |

---

## 给代码代理的执行顺序建议

1. **先按 U1–U6 修规格与数据层**（M1），跑通全部 config 解析与单位换算测试
2. **再按 U7–U10、U14 实现内层 MILP**（M2），注意 normal / fault 两套约束
3. **按 U15、U16 实现韧性**（M2.5），注意删失处理
4. 之后按 U11、U17 实现外层与多保真，按 U18 的固定配置先跑通 M4
5. U19、U20 的要求贯穿始终，尤其在写测试与出图时

若在实现中再发现规格冲突，沿用本次的做法：**停下来报告，不要自行选择实现方式**。
