# SPEC 决策附录 · 第四批（v1.4）

本文件回应代码代理第四轮复核提出的 2 处能量上限问题（下称 T1–T2）。

**权威性**：`SPEC.md` < `SPEC-REVISIONS.md`(v1.1) < `SPEC-REVISIONS-2.md`(v1.2)
< `SPEC-REVISIONS-3.md`(v1.3) < **本文件**。

**修订日期**：2026-10-10

**修订记录**：v1.4.1（2026-10-11）修正 §X3 的不变量表述与 §X4 的测试说明。
v1.4.0 把不变量写成 `soc(t+1) ∈ [soc_lo(T(t)), soc_hi(T(t))]`，**过强**，
在"起点已在带外"时会误报失败，且与同一节中"温度骤降时 SOC 高于上界是允许的"
这句自相矛盾。已改为**条件化不变量**（见 X3）。γ 机制与能量上限两式不变。

**范围**：仅修正 §X3 的不变量与 §X4 的测试断言。W1 的 γ 机制、X1/X2 的能量上限两式、
W2（冷热两侧拆分）、W3（168 小时代表周）**全部不变**。

---

## 总览

| # | 问题 | 性质 | 处置 |
|---|---|---|---|
| T1 | 附录公式漏正部截断 `[·]⁺`，越带后不可行 | 附录与源码不一致 | X1 修正 |
| T2 | 母线侧能量上限漏效率换算，会穿过可用下界 | **规格错误** | X2 修正 |

两条合并为**一组最终公式**（见 X3）。

---

## X1 · 正部截断（应 T1）

T1 正确。§W1 附录中的两式漏了 `max(0, ·)`：

```
（v1.3 附录的写法，有误）
e_ch  ≤ E_nom · [ soc_hi(T) − soc ]
e_dis ≤ E_nom · [ soc − soc_lo(T) ]
```

反例验算（T1 给出）：E_nom = 100、soc = 0.8、γ = 0.4 → soc_hi = 0.5 + 0.4×0.4 = 0.66

```
0 ≤ e_ch ≤ 100 × (0.66 − 0.8) = −14
```

`e_ch ≥ 0` 与 `e_ch ≤ −14` 冲突 → **不可行**。

`src/aidc/model/storage.py::energy_limits` 的 docstring 中已经写了 `max(0, ·)`，
是附录未同步——实现层是对的，规格层是错的。以源码为准，附录同步。

**物理含义**：温度骤降使 `soc_hi` 低于当前 SOC 时，**充电能力冻结为 0**，
而不是"必须充电到负值"。这正是"约束加在能量流而非状态上"的用意——
状态保持合法，只是某个方向的能力被冻结。

---

## X2 · 母线侧与电池侧的能量换算（应 T2）

T2 正确，且这是本项目反复强调的一类错误。**母线侧能量与电池侧 SOC 变化之间差一个效率。**

### 反例验算

设 E_nom = 100、soc = 0.5、soc_lo = 0.3、η_dis = 0.9：

```
旧式（漏效率）：e_dis ≤ 100 × (0.5 − 0.3) = 20 MWh
按 SOC 动态：    soc_next = 0.5 − 20/(0.9 × 100) = 0.2778 < 0.3   ✗ 穿过可用下界

新式：          e_dis ≤ 0.9 × 100 × (0.5 − 0.3) = 18 MWh
                soc_next = 0.5 − 18/(0.9 × 100) = 0.3             ✓ 恰停在下界
```

充电侧同样有偏差，方向相反（保守而非越界）：

```
设 soc = 0.5、soc_hi = 0.6、η_ch = 0.95
旧式：e_ch ≤ 10 → soc_next = 0.5 + 0.95×10/100 = 0.595，永远到不了 0.6（可用带被浪费 5%）
新式：e_ch ≤ 10/0.95 = 10.526 → soc_next = 0.5 + 0.95×10.526/100 = 0.6   ✓
```

### 推导

SOC 动态（不含 γ）：

```
soc(t+1) = soc(t) + [ η_ch·e_ch(t) − e_dis(t)/η_dis ] / E_nom
```

要求 `soc(t+1) ∈ [soc_lo, soc_hi]`：

```
充电侧：η_ch·e_ch ≤ E_nom·( soc_hi − soc )    ⟹  e_ch ≤ (E_nom/η_ch)·( soc_hi − soc )
放电侧：e_dis/η_dis ≤ E_nom·( soc − soc_lo )  ⟹  e_dis ≤ η_dis·E_nom·( soc − soc_lo )
```

再加非负性与正部截断，即得 X3 的最终式。

---

## X3 · 最终公式（替换 v1.3 §W1 的能量可用性约束）

以下**取代** v1.3 §W1 中"能量可用性约束"一节的全部内容。W1 的其余部分
（γ 的定义、可用带以 soc_ref 为中心、SOC 动态不含 γ、功率上限不乘 γ）**不变**。

### 可用带（不变）

```
half_band_x = 0.5 · ( soc_max − soc_min )
soc_hi_x(T) = soc_ref_x + γ_x(T) · half_band_x
soc_lo_x(T) = soc_ref_x − γ_x(T) · half_band_x
soc_ref_x   = soc_init_x = 0.50
```

### 能量上限（修正）

```
0 ≤ e_ch_x(t)  ≤ ( E_nom_x / η_ch_x(T) ) · [ soc_hi_x(T(t)) − soc_x(t) ]⁺
0 ≤ e_dis_x(t) ≤   η_dis_x(T) · E_nom_x  · [ soc_x(t) − soc_lo_x(T(t)) ]⁺

其中 [x]⁺ = max(0, x)，e_ch = p_ch·Δt，e_dis = p_dis·Δt（均为母线侧能量，MWh）
```

### SOC 动态（不变）

```
soc_x(t+1) = soc_x(t) + [ η_ch_x(T)·e_ch_x(t) − e_dis_x(t)/η_dis_x(T) ] / E_nom_x
```

### 时点约定（明确，防止 T1 类歧义再现）

**可用带在时段起点 t 求值**，即用 `soc_hi(T(t))` 与 `soc_lo(T(t))`，不是 `T(t+1)`。

由此得到的不变量是**条件化的**（v1.4.1 修正）：

```
soc_x(t+1) ∈ [ min( soc_x(t), soc_lo_x(T(t)) ) , max( soc_x(t), soc_hi_x(T(t)) ) ]    ∀ t
```

**为什么必须是这个形式**：v1.4.0 写成 `soc(t+1) ∈ [soc_lo(T(t)), soc_hi(T(t))]`，过强。
反例：E_nom = 100、soc = 0.8、γ = 0.4 → 可用带为 [0.34, 0.66]。
修正后的能量上限允许充放都取零，此时 `soc(t+1) = 0.8`，**合法但不在带内**。
即使强制放电，若额定功率只有 1 MW、η = 1，一小时后 SOC 最低只能到 0.79，
仍回不到 0.66。因此严格包含式会让降温场景误报失败——正是它要避免的不可行问题。

**该条件化不变量与能量上限等价**（推导见下），因此是紧的，不会漏掉真实违规。

**语义**（三条，实现与测试都按此理解）：

1. **起点在带内 → 终点仍须在带内。**
   `soc(t) ∈ [soc_lo, soc_hi]` 时，区间退化为 `[soc_lo, soc_hi]`。
2. **起点高于带 → 禁止充电，允许暂时留在带外。**
   `soc(t) > soc_hi` 时，上界冻结为 `soc(t)`（因为 `e_ch` 被正部截断为 0），
   允许 SOC 保持不动或逐步放电回带内。
3. **起点低于带 → 禁止放电，允许暂时留在带外。**
   `soc(t) < soc_lo` 时，下界冻结为 `soc(t)`（`e_dis` 被截断为 0），
   允许保持或逐步充电回带内。

**推导（为什么是紧的）**

由能量上限与 SOC 动态：

```
soc(t+1) = soc(t) + [ η_ch·e_ch − e_dis/η_dis ] / E_nom

上界：e_dis 取 0、e_ch 取最大 (E_nom/η_ch)·[soc_hi − soc(t)]⁺
      soc(t+1) ≤ soc(t) + [ soc_hi − soc(t) ]⁺ = max( soc(t), soc_hi )

下界：e_ch 取 0、e_dis 取最大 η_dis·E_nom·[soc(t) − soc_lo]⁺
      soc(t+1) ≥ soc(t) − [ soc(t) − soc_lo ]⁺ = min( soc(t), soc_lo )
```

两端同时取到，故区间紧。

**三种情形的验算**（E_nom = 100、soc_ref = 0.5、half_band = 0.4、γ = 0.4 → 带 [0.34, 0.66]）

| `soc(t)` | `e_ch_max` | `e_dis_max` | 允许的 `soc(t+1)` | 条件化区间 |
|---|---|---|---|---|
| 0.5（带内） | 16 | 16 | [0.34, 0.66] | `[min(0.5,0.34), max(0.5,0.66)]` = [0.34, 0.66] ✓ |
| 0.8（高于带） | 0 | 46 | [0.34, 0.80] | `[min(0.8,0.34), max(0.8,0.66)]` = [0.34, 0.80] ✓ |
| 0.2（低于带） | 46 | 0 | [0.20, 0.66] | `[min(0.2,0.34), max(0.2,0.66)]` = [0.20, 0.66] ✓ |

### 可用带的上下截断（防 γ 缩放越界）

`scale_range` 上界为 1.1，因此 γ 可能 > 1。此时 `soc_hi = soc_ref + γ·half_band`
会超过 `soc_max`（γ = 1.1 时 0.5 + 0.44 = 0.94 > 0.9）。**必须截断**：

```
soc_hi(T) = min( soc_max , soc_ref + γ(T)·half_band )
soc_lo(T) = max( soc_min , soc_ref − γ(T)·half_band )
```

截断后仍保证 `soc_ref ∈ [soc_lo, soc_hi]`（γ ≥ 0，且 soc_ref ∈ [soc_min, soc_max]），
因此初始状态恒可行、终端约束 `soc(0) = soc(T_end) = soc_ref` 恒可满足。

### 标称窗口的冗余性

因为 `soc(t+1)` 的条件化区间两端都在 `[soc_min, soc_max]` 内
（`soc(t)` 本身受标称窗口约束，`soc_lo`/`soc_hi` 已被截断），
标称窗口约束自动成立。

**仍建议保留标称窗口约束作为防御性冗余约束**，用于捕捉实现错误。

---

## X4 · 测试更新（替换 v1.3 §W1 的测试）

### 需要修改的既有测试

**`test_cumulative_discharge_capacity_derated`** —— 期望值需按效率区分：

```
η_dis = 1.0, γ = 0.5, E_nom = 100, soc_ref = 0.5, soc_lo = 0.3
    → 累计母线侧可放出 = 1.0 × 100 × 0.2 = 20 MWh

η_dis = 0.9（新增断言）
    → 累计母线侧可放出 = 0.9 × 100 × 0.2 = 18 MWh
```

统一表述：**累计可放出（母线侧）= η_dis · E_nom · ( soc_ref − soc_lo )**，
代入 `soc_lo = soc_ref − γ·half_band` 得 `η_dis · γ · E_nom · half_band`。
直接算前者即可，避免记忆简化式。

**`test_charge_discharge_roundtrip_is_lossless`** —— 该测试**仅在 η = 1 时成立**，
docstring 需注明：η_ch 或 η_dis < 1 时，充放相同母线能量后 SOC 应净下降
（下降量对应往返损耗），这是正确行为而非 bug。

### 新增测试

**`test_energy_limits_positive_part`**（守 T1）

```
构造：soc = 0.8（已在带外上方）、γ = 0.4 → soc_hi = 0.66
断言：e_ch_max = 0（而非负数 -14）
     且该场景求解成功，不是 infeasible
```

**`test_bus_side_limits_respect_efficiency`**（守 T2）

```
构造：η_dis = 0.9, E_nom = 100, soc = 0.5, soc_lo = 0.3
断言 1：e_dis_max = 18（而非 20）
断言 2：按该上限放电后 soc_next = 0.3（容差 1e-9），不穿过下界
对称检查充电侧：η_ch = 0.95 时 e_ch_max = 10/0.95（而非 10）
```

**`test_soc_stays_within_usable_band`**（全时段不变量，两个方向都守）

⚠️ **该测试必须按条件化不变量写**（v1.4.1 修正），不能写成 `soc(t+1) ∈ [soc_lo, soc_hi]`。

```
对每条正常模式轨迹逐时段断言：

    # 主不变量（条件化，与能量上限等价）
    lo_eff = min( soc(t)   , soc_lo(T(t)) )
    hi_eff = max( soc(t)   , soc_hi(T(t)) )
    assert lo_eff - 1e-9 <= soc(t+1) <= hi_eff + 1e-9        ∀ t

    # 三条语义附加断言
    if soc(t) > soc_hi(T(t)) + 1e-9:      # 起点高于带
        assert e_ch(t) == 0                                   # 禁止充电
    if soc(t) < soc_lo(T(t)) - 1e-9:      # 起点低于带
        assert e_dis(t) == 0                                  # 禁止放电
    if soc_lo(T(t)) <= soc(t) <= soc_hi(T(t)):                # 起点在带内
        assert soc_lo(T(t)) - 1e-9 <= soc(t+1) <= soc_hi(T(t)) + 1e-9

    # 始终检查的独立约束
    assert soc_min <= soc(t+1) <= soc_max                     # 标称窗口
    assert soc(t+1) == pytest.approx(                         # SOC 动态
        soc(t) + ( eta_ch*e_ch(t) - e_dis(t)/eta_dis ) / E_nom, abs=1e-9 )
    assert e_ch(t)  <= p_ch_max(T(t))  * dt + 1e-9            # 功率上限
    assert e_dis(t) <= p_dis_max(T(t)) * dt + 1e-9
    assert e_ch(t)  <= (E_nom/eta_ch)*max(0, soc_hi(T(t)) - soc(t)) + 1e-9   # 母线侧能量上限
    assert e_dis(t) <= eta_dis*E_nom*max(0, soc(t) - soc_lo(T(t))) + 1e-9
```

**注意 `T(t)` 而非 `T(t+1)`**（§X3 的时点约定）。

⚠️ 这条测试同时守住 T1（正部截断）与 T2（效率换算）：
若漏正部截断或漏效率，条件化区间的某一端会被突破，此断言失败。
轨迹中已含 `soc_lo_Na` / `soc_hi_Na` / `soc_lo_Li` / `soc_hi_Li` 列，直接使用即可。

**`test_temperature_drop_does_not_make_infeasible`** —— 补充断言（守本次修正）

```
构造：先在 γ = 1 时充到 soc = 0.8，再令温度骤降至 γ = 0.4（带变 [0.34, 0.66]）
断言：
    1. 求解成功（不是 infeasible）
    2. 降温时段 e_ch = 0（充电冻结）
    3. **该时段 soc(t+1) 允许等于 0.8**（留在带外），
       即不要求 SOC 立刻回到带内——这是 v1.4.0 断言过强之处
    4. 若后续时段温度仍低且给定足够时间，SOC 应逐步降至带内（不强制，仅记录）
```

---

## 附：需要同步修改的文件

| 文件 | 修改点 |
|---|---|
| `SPEC-REVISIONS-3.md` §W1 | 能量可用性约束两式按 X3 修正（已在文中就地标注） |
| `src/aidc/model/storage.py` | `energy_limits` 增加 `eta_ch` / `eta_dis` 输入，公式按 X3 |
| `tests/test_invariants.py` | 按 X4 修改两条、新增三条 |

---

## 给代码代理的确认

1. **T1、T2 全部成立**。T1 是附录与源码不一致（源码对、附录错），T2 是实质错误。
2. **采用你提出的公式**，与 X3 完全一致：

   ```
   0 ≤ e_ch  ≤ (E_nom/η_ch)·[soc_hi − soc]⁺
   0 ≤ e_dis ≤ η_dis·E_nom·[soc − soc_lo]⁺
   ```

   你关于"这同时处理越带后的方向冻结和效率换算"的判断正确。
3. **γ 机制保留不变**（γ 只作用于可用带、不进入 SOC 动态）。
4. 已同步 `storage.py` 的 `energy_limits` 签名与 `test_invariants.py`，
   并补充 X3 的时点约定——该约定是 T1 歧义的另一半来源，必须写死，
   否则不变量测试会误报。
5. W2、W3 无需更改，确认。
6. 按本文件修正后即可开始 M1。M1 守门测试 = v1.3 的 W1 五条（按 X4 调整）
   + 代表周两条 + 原有配置解析/单位换算/曲线结构/任务边界。
7. 若在实现中再发现冲突，仍按原约定：**停下来报告，不要自行选择实现方式**。
