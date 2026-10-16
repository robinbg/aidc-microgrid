# Codex 触发提示词

**分四段使用。每次只发一段**，等它跑完测试、你看过产出后再发下一段。
不要一次性把 M1–M8 全丢过去。

规格现在只有一份：**`SPEC.md`（v2.10）**。`SPEC-REVISIONS*.md` 是历史留档，
**不要让它去读**（含作废结论，会被误导）。

---

## 阶段零：先对齐理解（不发任务）

```
你是一名资深 Python 工程代理。工作目录是 09-aidc-microgrid-kickoff/。

在写任何代码之前，请先做三件事：

0. **先跑一致性自查，把结果贴给我**：

       python tools/selfcheck.py

   它检查：引用完整性、§9 编号唯一性、测试名同步、版本一致性、
   关键常量一致、废弃概念、§9.A/§9.B 分级与 `@pytest.mark.solver` 是否吻合、
   轨迹列清单是否与源码一致。
   ⚠️ **若报 ERROR，先停下报告，不要带着不一致开始实现。**

1. 完整阅读 SPEC.md（v2.10，唯一权威规格）。
   再读 README.md、CHANGELOG.md，以及 configs/ 下全部 YAML。
   ⚠️ 不要读 SPEC-REVISIONS*.md —— 那是历史留档，含已作废结论。

2. 用你自己的话复述以下十点，逐条确认你理解正确：
   - §1.1 的单位换算表（配置单位 → 内部单位，因子各是多少）
     **以及"换算只发生一次、在加载层"的含义**
   - §1.5 的参数唯一真源三层检测（为什么不做裸参数名检查；
     为什么 `derived_from` 必须可解析而不是自然语言；
     为什么依赖链要如实展开、不能抄近路）
   - §2.2 的 `IT_load` 与 `IT_served` 的区别（为什么故障模式下必须分开）
   - §2.3 的 SOC 动态与可用带：γ 出现在哪个式子里、**不出现在哪个式子里**
   - §2.3 的条件化不变量（为什么不能写成 soc(t+1) ∈ [soc_lo, soc_hi]）
   - §2.4 的供热分配：为什么 **min/max 不能直接写成约束**（图非凸），
     **为什么"目标系数为正"不能代替约束**（SPEC §2.4 有反例），
     以及单二进制 `y(t)` 各条约束起什么作用
   - §2.4 里**被否决的 `z_boiler`** 与**本节的 `y(t)`** 区别在哪里
   - §5.2 的关键负荷为什么是"设施侧"口径，以及 §5.2b 的 EENS 统计范围
   - §3.4 每个策略目标中各成本项的**符号**（最小化口径下烧气为正）
   - §4.3 的 LCOE 分母为什么是"年服务电量"而不是"储能放电量"

3. 列出你在 SPEC.md 中发现的任何矛盾、遗漏或无法实现的地方。
   如果你认为某处规格有问题，直接指出，不要默默按自己的理解实现。
   ⚠️ **请给出反例或验算过程**，不要只报结论——
   本项目历史上出现过"结论对但理由错"的修正，那种错误最隐蔽。

不要写代码。只做阅读与复述。完成第 3 条后停下等我确认。
```

---

## 阶段一：实现 M1（数据与参数层 + 纯函数）

```
现在实现 M1。范围是**四个模块**：

1. src/aidc/data/chemistry.py
   （load_chemistry、Curve 求值、usable_band、power_bounds、eta_charge/discharge、
     apply_lithium_price_scenario）
2. src/aidc/data/timeseries.py
   （build_year_timeseries 生成 8928 h、select_representative_weeks、
     verify_capacity_factors、slice_window）
3. src/aidc/data/tasks.py
   （check_flexible_ratio_feasible、generate_tasks、summarize_tasks）
4. src/aidc/model/storage.py 的**纯函数**部分
   （soc_step、usable_band、energy_limits、equivalent_cycles、
     area_requirement、capacity_fade、replacement_cost_annual）
   以及 src/aidc/model/thermal.py 的**纯函数**部分
   （waste_heat、recoverable_heat、heat_output、allocate_heat、
     heat_source_balance、hp_power_limit、backup_heat_output、
     verify_municipal_delivery）

**这些纯函数不需要求解器**，因此属于 M1。

约束：
1. 严格遵守 SPEC §1.1 的单位换算表。所有参数从 configs/ 读取，禁止硬编码。
   配置用元/kWh、元/kW、kW/卡 等单位，**加载时必须换算**为元/MWh、元/MW、MW/卡。
   ⚠️ **换算只发生一次，且在加载层**。公式内不得再出现任何 ×1000 / ÷1000。
   `ChemistryParams` 的字段已按内部单位命名（area_per_mwh、unit_cost_energy 等），
   按字段名使用即可，不要手工换算。
2. ⚠️ **禁止硬编码 `0.9333`**。可产热上界的系数是
   `COP/(COP−1) · η_rec = 14/15`，必须由 config 的 cop 与 eta_recovery 现算。
3. **不要修改 configs/ 下的任何 YAML 文件**。它们是规格方提供的。
   如果发现参数缺失，报告给我，不要自己补。
4. **不要实现 M2 的 `dispatch.py`**。也不要为了让测试通过而先写一个假的求解器。
5. 随机数一律用 numpy.random.default_rng(seed)，禁止使用全局 np.random。

完成后的验收（必须全部通过；需要求解器的测试会自动 skip，这是预期的）：

    pytest tests/test_chemistry_params.py -v
    pytest tests/test_invariants.py -v -m "not solver"

其中以下几条的期望值已在 SPEC §9 给出，请严格按此实现（不要自己改期望值）：

- test_cumulative_discharge_capacity_derated：γ = 0.5、E = 100、soc_ref = 0.5
  → 累计可放 **20 MWh**（η = 1）/ **18 MWh**（η_dis = 0.9），**不是 40**
- test_energy_limits_positive_part：E = 100、soc = 0.8、γ = 0.4
  → `e_ch_max = 0`（**不是负数 −14**）
- test_bus_side_limits_respect_efficiency：η_dis = 0.9 时 `e_dis_max = 18`
- test_usable_band_scales_with_gamma：γ = 1 → [0.1, 0.9]；γ = 0.5 → [0.3, 0.7]
- test_flexible_ratio_feasible_pretest：四画像的 u_req 分别为
  **0.372 / 0.468 / 0.273 / 0.333**（均 ≤ 0.60）

然后用一段脚本做三条自检：
  a) 生成 R-NW 的 8928 h 序列，打印 T_min、T_max、实际风电与光伏容量因子，
     并与 regions/r_nw.yaml 的参考值比较（**只用前 8760 h 计算**）
  b) 计算 R-NW 代表周的 E_S1，按 flexible_ratio = 0.65 生成任务，
     打印 E_S1 / E_tasks / E_IT 三项电量与实际柔性占比
  c) 验证光伏出力在夜间严格为 0

最后汇报：
  - 改动的文件清单
  - 测试结果（含被 skip 的求解器测试清单）
  - 上述三条自检的实际输出
  - 你做的任何 SPEC 未明确规定、由你自己决定的实现选择（**逐条列出**）
  - 标定进度：chemistry_*.yaml 与 generation.yaml 中还有多少项是 PLACEHOLDER

完成后停下。
```

---

## 阶段二：实现 M2（内层 MILP，双模式）

```
现在实现 M2 · 内层 MILP，范围限定在 src/aidc/model/ 下四个模块：
dispatch.py、storage.py、thermal.py、carbon.py。

这是整个项目的核心。实现要求：

1. 严格遵守 SPEC §2–§3 的物理模型与约束，逐条对照，不要简化。
2. **两个模式都要实现**（SPEC §3.6）：
   - mode="normal"：需求全满足、任务全完成、SOC 首末相等、市政完全补足
   - mode="fault"：引入 unserved_elec / it_shed / unserved_heat、取消 SOC 终端约束、
     可迁移任务全部暂停、用 SPEC §3.7 的降级策略而非经济目标
   ⚠️ 故障模式三处最容易错（只在故障轨迹上显现，正常轨迹完全正常）：
     a) 制冷与余热必须按 `IT_served = IT_load − it_shed` 算，**不是 `IT_load`**
        ⚠️ 也**不是** `IT_load − unserved_elec`：`it_shed` 是 IT 侧量，
        `unserved_elec = PUE·it_shed` 是母线侧量，差一个 PUE 因子
        —— 否则停电时仍算出正的 Q_rec
     b) 市政交付式改为 `Q_mun_hp + Q_boiler + unserved_heat = demand`
        且 `Q_boiler ≤ min(容量, 缺口)`，**不能**沿用正常模式的"完全补足"
     c) 不要让 `unserved_elec` 与 `IT_served` 各自独立取值——两者由 (a) 的等式绑定
   两套约束不可混用。

2b. **降级策略的两条规则已裁定，不要再抛 ValueError 来问我**（SPEC §3.7 与 §3.5）：

   - **故障顺序放电 = 优先级顺序，不是逐时互斥。** 同一小时内 Na 先放，
     Na 触及功率/能量上限后 **Li 在同一小时内接续补足差额**；
     两体系**可以**在同一小时同时出力。**不存在"每小时只许一种体系"这一读法**——
     若 Na 不足而 Li 有余却削减关键负荷，会造出**假孤岛**
     （`T_island = 0`，但原因不是物理，而是实现选择）。
     为保证确定性（§9.39），在 `z1 = z1*` 的**最优面上**加 **1b 级**：
     `min Σ_t P_disch_Li(t)·Δt`。
     ⚠️ **只能在最优面上最小化**，不能写成第一级里的 `+ε·Σ p_dis_Li`——
     正系数放进第一级会让求解器用"多削减关键负荷"去换"少放 Li"
     （与 §3.7 容差反例是**同一个机制**）。
     ⚠️ **偏好排在全部服务目标之后（第 4 级）**，不在非关键负荷之前（v2.8 修正）：
     先固定 `Li = 0` 会限制总供电能力，在"Na 不足、需 Li 补足"时会**削减非关键负荷**，
     即用供电换循环寿命。故 `Li ≈ 0` 只在 **Na 充裕**场景成立。
     ⚠️ 第 4 级未证明最优**不**触发 `feasible_not_proven`——它只决定
     "由哪一体系供同样多的负荷"，不改变 `shed_c`/`shed_nc`（§3.8b 只约束第一级）。
     ⚠️ 另注意：`economics.yaml` 称按 `cycle_life` 保护更长寿命的体系，
     但当前 Na 与 Li 的 `cycle_life.mid` **都是 6000**，该理由暂不成立（标定后复核）。

   - **S-base 储能 = 电网优先、储能只补容量缺口**（SPEC §3.5）。逐时顺序：
     ① 可再生先自用（不经储能）
     ② 缺额**先由电网补足**，上限 `import_capacity`
     ③ **仅当缺额超过进口容量**时，差额由储能放电补足
     ④ 盈余先充储能（受功率与可用带上限），充不进的记弃电
     当前配置（`import_capacity = 30 MW` 而 `IT_base ≤ 1.08 MW`）下电网几乎永不为瓶颈，
     **S-base 退化为近似 A0 的基线是预期结果**——不要为了让储能"动起来"而改这条规则。
     另：S-base 与 SOC 终端约束的交互按 §3.5 的固定处置实现
     （① 盈余充电与 ② 电网补电**都只充到 `soc_ref` 为止**，
     **并叠加 ⓪ 终端平衡预充** `E_need = D_forced/(η_ch·η_dis)`；
     电网补电时段取窗口内 `price_buy` 最低的若干小时，
     并把 `s_base_surplus_charge_mwh`、`s_base_grid_charge_mwh`、
     `s_base_terminal_reserve_mwh`、`s_base_forced_discharge_mwh` 写入结果表），
     **不要自行发明**（§9.28 会查）。
     ⚠️ **只约束 ① 与 ② 是不够的**（v2.9 补 ⓪）：
     规则 ③ 的**强制放电**会从下方破坏终端相等。反例（两小时、`η = 0.978`、`E = 100`）：
     bus 需求 `[0.96957, 1.72557] MW`、电网容量恒 `1.5 MW` → t=2 强制放电 0.22557 MWh
     → 末态 SOC `0.49769356 ≠ 0.5`，而**没有任何规则允许回升**。
     **⓪ 正是补这个缺口**：提前充 `0.22557/0.978² = 0.23583249 MWh`
     （峰值 SOC `0.50230659`，**在可用带 `[0.3, 0.7]` 之内**，t=1 进口 `1.2054 < 1.5`）。
     ⚠️ **不得把 `soc_ref` 之上说成"不可用"**：按 §2.3 对称带，它**是**可用容量；
     "只充到 `soc_ref`"是 **S-base 的策略约束**，不得改进 `usable_band`，
     也不得影响其余四条策略。
     ⚠️ **"不得返回 `infeasible`"只针对"规则自致"**；
     `unserved_elec > 0`（物理容量确实不足）**必须原样报告 infeasible**。

3. **低温折减的实现（SPEC §2.3，最容易错的地方）**：
   - SOC 动态**不含 γ**
   - γ 只作用于可用带：soc_hi/soc_lo 以 soc_ref 为中心对称收缩，**含上下截断**
   - 能量上限两式必须含**正部截断** `[·]⁺` 与**效率换算**（e_ch ≤ (E_nom/η_ch)[·]⁺、
     e_dis ≤ η_dis·E_nom·[·]⁺）
   - 功率上限 min(P_nom, c_max·E_nom)，**不乘 γ**
   - 可用带在**时段起点 t** 求值
4. **供热分配必须用每时段一个二进制 `y(t)`，且 `M` 取常数差额上界**（SPEC §2.4）：
   - ⚠️ **不要**用"目标中 Q_boiler 系数为正"来代替约束——**该条件是错的**，
     反例见 SPEC §2.4（R-EAST 参数下"工业优先"与"市政优先"目标值完全相同）
   - ⚠️ **也不要**取 `M = Q_hp_max(IT_served)`——它有三处错
     （界不足导致两分支都不可行、`M·y` 成双线性项、分支标签反向）
   - 编码（`U(t)` 取满载常数界）：
         y(t) ∈ {0,1}；y=0⟺H≤D，y=1⟺H≥D
         Q_mun_hp ≤ Q_hp；Q_mun_hp ≤ Q_mun_demand
         Q_mun_hp ≥ Q_hp          − max(0, U(t)−D(t)) · y(t)
         Q_mun_hp ≥ Q_mun_demand  − Q_mun_demand(t) · (1 − y(t))
         U(t) = min(COP/(COP−1)·η_rec·IT_full, Q_mun_demand+Q_ind_capacity) [数据常数]
   - **但要保留** `S-cost`/`S-heat` 的锅炉燃料成本项（它守住另一个退化）
   - `Q_ind` 一侧**不需要**第二个二进制（其边际价值在五个策略中恒为正，
     靠目标压力即可）——这是真正的"无需二进制"，且不依赖标定
   - 热泵出力受**双侧**上限（热源侧与需求侧取小，**无 heat_tolerance**）
5. 余热口径 κ = 0（只计 IT 侧排热）。
6. 五个策略各有**独立的标量目标**（SPEC §3.4–3.5），不是权重组合：
   - ⚠️ **`S-cost` 与 `S-heat` 都必须含锅炉燃料成本** `+Q_boiler·boiler_unit_cost`，
     `S-heat` 还要含热泵电费 `+P_hp·price_buy`
   - 符号口径：**最小化**目标下"烧气"是**正项**、"卖热"是**负项**。
     数例见 §3.5（E 得 86、F 得 320）
   - 市政供热收入只在 `price_mun_is_external = true` 时计入（默认 false）
   - S-carbon 用 `sla_floor` 硬约束，不把"元"惩罚加进"kgCO₂"目标
   - SLA_penalty_week 在 Σ_t 之外，整窗口只计一次
   - `S-balance` 的三项权重在 `experiments.yaml` 的 `strategies[S-balance].weights`；
     `w_sla` 的唯一真源是 `sla.w_sla_balance`
   - 每个目标末尾加 ε = 1e-6 的辅助项（SPEC §2.4 五），
     **并声明这是加权近似而非严格字典序**
7. SOC 终端约束（仅 normal 模式）必须有，防止"掏空电池"。
8. 面积约束必须作为硬约束进入 MILP。⚠️ `area_per_mwh` 已是 m²/MWh，
   **公式内不要再乘 1000**。
9. 求解前做可行性预检；infeasible 必须原样返回并标记，**不得返回 0 成本**。

验收（全部必须通过）：

    pytest tests/test_invariants.py -v

其中 **test_hand_calculated_small_case 最重要**：构造 4 个时段、1 个任务、
无风光、单一储能的最小场景，手算最优成本与 SOC 轨迹，与求解器逐项对照。
**请把手算过程写出来**，便于我复核。

另外两条必过：`test_boiler_cost_coefficient_positive`（目标中 Q_boiler 系数 > 0）
与 `test_fault_served_load_chain`（故障模式的被供负荷链条）。

再跑一个端到端冒烟：R-NW 前 168 h，配置 A3（E_Na=100, P_Na=30, E_Li=50, P_Li=15），
策略 S-cost，跑通一次完整调度并打印：
  - 求解状态、耗时、MIP gap、变量数
  - 关键指标（成本、净碳排、SLA 达标率、绿电匹配率、弃电率、弃热总量）
  - SOC 的最小值与最大值，以及 max |Q_dump|（应 < 1e-9）

汇报：
  - 改动的文件清单与测试结果
  - 手算对照的过程与结论
  - 冒烟运行的完整输出
  - 你做的任何 SPEC 未明确规定的实现选择
  - 实现中发现的 SPEC 问题

完成后停下，等我确认再进入 M3。
```

---

## 常见情况怎么处理

**它想一次做完 M1–M8**：回一句"只做当前阶段，其余的等我下发"。不要让它自由发挥。

**它想读 `SPEC-REVISIONS*.md`**：明确制止——那九份是历史留档，含已作废结论
（例如"γ 进入 SOC 动态""反互补约束""24 h 代表日"都是错的），读了会写错。

**它自己改了测试**：要求它报告对 `tests/` 的任何改动并说明理由。
测试是验收门槛，被改动的测试要单独复核。

**它报告 SPEC 有矛盾**：认真看。这份 SPEC 经**十一轮**复核，
但**两轮阶段零复核各找出 9 条、全部成立**——包括我在合并时新引入的错误。
"复核过多轮"不等于没有错，**它的每一条都要独立验算**。

**四个配置臂结果几乎一样**：先查 README「最容易踩的坑」第 1、4、5 条——
γ 写进了动态、参数没进约束、PUE 被设成常数。

**它改了验收命令的 `-k` 过滤器**：M1 阶段用 `-m "not solver"`，
M2 阶段用完整命令。不要让它用 `-k` 挑测试——那样会漏跑新增的测试。

**它报告供热分配恒等式（9.17）失败**：**先查二进制 `y(t)` 是否已加入约束**，
其次查 `M(t)` 是否取了逐时紧上界（取大数会让分支松弛失效，但不至于算错）。
**不要**再去查"锅炉成本项是否漏了"——那是 v2.1 的错误引导。
（锅炉成本项仍然要查，但它守的是另一个退化，见 9.29。）

**它说"min/max 不能写成约束，所以改用目标单调性"**：制止。
这是 v2.1 犯过的错，且该错误**能通过系数符号检查**（所以特别隐蔽）。
`min` 的精确编码就是**一个二进制**；"避免二进制"本身不是目标。
**但也要制止它退回 `z_boiler` 那套**——那个二进制编码的是
"是否允许烧气"这个业务许可，与编码 `min` 本身是两回事。

**它想把 `M` 取成 1e6 之类的大数，或取 `Q_hp_max(t)`**：制止两者。
正确取法是**常数差额上界** `M_1 = max(0, U−D)`、`M_2 = D`，
`U(t)` 用满载常数界（§2.4）。取大数会让分支松弛失效、求解变慢；
取 `Q_hp_max(IT_served)` 会**界不足**（`M < D−M` 时两分支都不行）且引入双线性项。

**它说某个测试期望值不对**：先自己复核 SPEC §9 的期望值表。这些值都经过手算，
若它认为有误，要求它给出反例推导，不要直接改。

---

## 一句话总控

如果只想发一句话启动：

```
读 09-aidc-microgrid-kickoff/SPEC.md（唯一权威规格，v2.10）与 README.md，
不要读 SPEC-REVISIONS*.md（历史留档，含作废结论）。
然后只实现 M1：src/aidc/data/ 下三个模块，加上 model/storage.py 与 model/thermal.py
的**纯函数**部分（soc_step、usable_band、energy_limits、area_requirement、
waste_heat、recoverable_heat、heat_output、allocate_heat、heat_source_balance、
hp_power_limit、backup_heat_output、verify_municipal_delivery）。
**不要实现 dispatch.py**，也不要为了让测试通过而先写假的求解器。

实现前先复述 SPEC §1.1 的单位换算表（以及"换算只发生一次、在加载层"）、
§1.5 的唯一真源三层检测、§2.2 的 IT_load 与 IT_served 的区别、
§2.3 的 SOC 动态与可用带（γ 出现在哪里、不出现在哪里）、
§2.4 的供热分配为什么不能用 min/max 直接写约束、
§3.4 各策略目标中成本项的符号、§4.3 的 LCOE 分母口径，
等我确认。

完成后跑 pytest tests/test_chemistry_params.py 与
tests/test_invariants.py -m "not solver"
并全部通过（需求解器的测试会自动 skip，这是预期的），
汇报改动文件清单、被 skip 的测试清单、你的实现选择、以及标定进度。

不要动 configs/ 的 YAML，不要硬编码 0.9333（系数由 cop 与 eta_recovery 现算），
不要在公式里再做单位换算。
```
