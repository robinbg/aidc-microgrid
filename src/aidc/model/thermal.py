"""热泵、余热回收与供热分配（SPEC §2.4）。

⚠️ 表述红线（SPEC §10.2）：
    IT 侧排热是**低品位热源**，经热泵提温后作为市政/工业供热的**补充**，
    不得在任何输出中表述为"主要热源"。
"""

from __future__ import annotations

import numpy as np


def _nonnegative(value, name):
    array = np.asarray(value, dtype=float)
    if not np.all(np.isfinite(array)) or np.any(array < 0):
        raise ValueError(f"{name} 必须是有限非负数")
    return array


def _cop(cop):
    if not np.isfinite(cop) or cop <= 1:
        raise ValueError("COP 必须大于 1")
    return cop


def waste_heat(
    IT_served: np.ndarray, P_cool_served: np.ndarray, kappa: float = 0.0
) -> np.ndarray:
    """IT 侧可回收排热（MW_th）。

        W_it = IT_served + κ · P_cool_served

    ⚠️ **入参名即口径**（v2.5 改）：两个参数都是**实际被供**的量，不是需求侧。
    命名与 SPEC §2.1 的关键字一致，避免调用方误传 `IT_load`。
    故障模式下算力负荷可能被削减或完全失去供电，此时服务器不发热、也不需制冷；
    按需求侧计算会**在停电时算出正的余热**，虚高供热覆盖率与续航。
    正常模式下二者相等（`it_shed ≡ 0`），故该区分不改变正常轨迹。

    ⚠️ **κ 默认 0**：只计 IT 侧排热（冷板液冷/背板换热器回收 40–60℃ 中温热水）。
    制冷机组的冷凝热温度更低，通常直排环境，不回收。

    把 P_cool 计入（κ > 0）需要专门的水冷式冷凝器设计，且会引入虚假耦合：
    冷气候 → 低 PUE → 可用热更少，而冷气候同时意味着供热需求更高。
    κ = 0 使余热供给与 PUE 无关，消除该耦合。

    若启用 κ > 0，**必须在报告中说明依据**，不得默认开启。

    可回收余热与可产热上界：

        Q_rec    = η_rec · W_it
        Q_hp_max = COP/(COP−1) · Q_rec
                 = (4/3) · 0.7 · W_it = (14/15) · W_it     （COP = 4，η_rec = 0.7）

    ⚠️ 系数**由 cop 与 eta_recovery 现算，禁止硬编码 0.9333**。
    `0.9333` 是 `14/15 = 0.93333…` 的四位截断，在满载 4.3008 MW 处
    相差 1.4336e-4 MW_th；若用它做硬断言，会拒绝完全正确的满载解。
    """
    served = _nonnegative(IT_served, "IT_served")
    cooling = _nonnegative(P_cool_served, "P_cool_served")
    if not np.isfinite(kappa) or not 0 <= kappa <= 1:
        raise ValueError("kappa 必须在 [0, 1] 内")
    return served + kappa * cooling


def recoverable_heat(W_it: np.ndarray, eta_recovery: float) -> np.ndarray:
    """可回收热量 Q_rec = eta_recovery * W_it（MW_th）。"""
    if not np.isfinite(eta_recovery) or not 0 <= eta_recovery <= 1:
        raise ValueError("eta_recovery 必须在 [0, 1] 内")
    return eta_recovery * _nonnegative(W_it, "W_it")


def backup_heat_output(
    Q_gap: np.ndarray, q_capacity_mw_th: float
) -> np.ndarray:
    """备用（峰值）热源的出力（MW_th，SPEC §2.4）。

        Q_boiler = min( Q_gap , q_capacity_mw_th )

    其中 `Q_gap = Q_mun_demand − Q_mun_hp` 为市政刚性需求的缺口。

    **为什么需要备用热源**：AIDC 可回收余热上限受 IT 用电量约束
    （满载时约 4.01 MW_th），撑不起区域供热的峰值需求。供热系统历来配峰值锅炉，
    这是常规工程做法。

    同时它使方案一贯的表述——"余热经热泵提温后作为补充热源，不是主要热源"
    （SPEC §10.2）——在模型里**字面成立**：主要热源的角色由锅炉承担。

    ⚠️ **本函数给出的是事后核算规则，不是 MILP 里的约束**。
    求解时 `Q_boiler` 是决策变量，由下列约束限定（正常模式）：

        Q_mun_hp(t) + Q_boiler(t) = Q_mun_demand(t)          （交付恒等式）
        Q_boiler(t) ≤ q_capacity_mw_th

    ⚠️ **本函数是事后核算 / 验证用的分配规则，不是 MILP 的约束**（v2.2）。

    原因：`min()/max()` 的**图不是凸集**，不能作为线性约束的可行域。
    反例（`D` 为参数、`m = min(H, D)`）：点 `(0, 0)` 与 `(2D, D)` 都在图上，
    但中点 `(D, D/2)` 不在。

    MILP 中的正确写法是**每时段一个二进制 `y(t)`**（见 allocate_heat 与 SPEC §2.4）。

    ⚠️ **正常模式与故障模式的交付式不同**（SPEC §2.4）：

        normal: Q_mun_hp + Q_boiler = demand                 （完全补足）
        fault : Q_mun_hp + Q_boiler + unserved_heat = demand （允许缺口）

    故障模式若沿用"完全补足"式，`unserved_heat` 恒为 0，该变量形同虚设。

    ⚠️ 若某时段的 `Q_mun_demand − Q_mun_hp` 超过 `q_capacity_mw_th`，
    说明备用热源容量不足、市政刚性需求无法满足。这属于**配置不合理**，
    正常模式下应报错而不是静默截断。

    容量选择的依据：`q_capacity_mw_th` 应不小于各画像的市政峰值需求
    （当前最大为 R-NW 的 3.5 MW_th，配置值 4.0 MW_th）。

    Returns:
        Q_boiler（MW_th）
    """
    gap = _nonnegative(Q_gap, "Q_gap")
    _nonnegative(q_capacity_mw_th, "q_capacity_mw_th")
    if np.any(gap > q_capacity_mw_th):
        raise ValueError("备用热源容量不足，正常模式无法满足市政刚性需求")
    return np.minimum(gap, q_capacity_mw_th)


def verify_municipal_delivery(
    Q_mun_hp: np.ndarray,
    Q_boiler: np.ndarray,
    Q_mun_demand: np.ndarray,
    tol: float = 1e-6,
) -> tuple[bool, float]:
    """校验市政供热交付恒等式（SPEC §2.4）。

        Q_mun_hp(t) + Q_boiler(t) == Q_mun_demand(t)        逐时

    ⚠️ 这是"刚性需求"约束的完整形式。此前只有热泵一侧（`Q_mun`），
    在余热不足时必然不可行——这正是 AD1 指出的问题。

    ⚠️ 注意变量语义的区分：
        Q_mun_demand  网络侧需求（外生）
        Q_mun_hp      热泵供给市政的部分（内生决策）
        Q_boiler      备用热源补充的部分（内生决策）

    Returns:
        (是否全部满足, 最大绝对交付残差)，同时拒绝欠供与超供。
    """
    if not np.isfinite(tol) or tol < 0:
        raise ValueError("tol 必须是有限非负数")
    hp = _nonnegative(Q_mun_hp, "Q_mun_hp")
    boiler = _nonnegative(Q_boiler, "Q_boiler")
    demand = _nonnegative(Q_mun_demand, "Q_mun_demand")
    residual = float(np.max(np.abs(hp + boiler - demand), initial=0))
    return residual <= tol, residual


def verify_municipal_heat(
    Q_mun: np.ndarray, Q_mun_demand: np.ndarray, tol: float = 1e-6
) -> tuple[bool, float]:
    """校验市政供热刚性满足（旧接口，保留用于 fault 模式）。

    正常模式下应改用 verify_municipal_delivery（含锅炉项）。
    fault 模式下允许缺口，此时本函数用于确认缺口已被记录为 unserved_heat。

    Returns:
        (是否全部满足, 最大缺口)
    """
    return verify_municipal_delivery(Q_mun, np.zeros_like(Q_mun), Q_mun_demand, tol)


def hp_power_limit(
    Q_rec: np.ndarray,
    cop: float,
    Q_mun_demand: np.ndarray | None = None,
    Q_ind_capacity: np.ndarray | None = None,
) -> np.ndarray:
    """热泵电耗上限（MW）—— 同时受热源侧与需求侧约束。

        P_hp(t) ≤ min( Q_rec(t)/(COP−1) ,
                       [ Q_mun_demand(t) + Q_ind_capacity(t) ] / COP )

    - **热源侧**：热泵从热源吸热 Q_low = P_hp·(COP−1)，不得超过可回收余热。
      写成 `P_hp ≤ Q_rec` 会高估热泵能力（漏掉 COP−1）。
    - **需求侧**：产出的热量必须有去处。若该画像无任何供热接纳能力
      （如 R-SOUTH 的市政与工业接纳均为 0），则 P_hp 上限为 0——
      热泵不是必须满出力，这是正确的物理行为。

    ⚠️ **需求侧上限是独立硬约束，不是冗余**（v1.9 修正）：

        P_hp(t) ≤ [ Q_mun_demand(t) + Q_ind_capacity(t) ] / COP

    它把热泵出力限制在"可被有效吸收"的范围内。
    v1.8 曾称该上限在分配规则下"自动满足"，**该论断错误**——它忽略了 `Q_dump`：

        反例：需求 0.8、工业接纳 0、Q_hp = 1
        分配规则允许 Q_mun_hp = 0.8、Q_ind = 0、Q_dump = 0.2
        但上限给出 P_hp ≤ 0.8/4 = 0.2，即 Q_hp ≤ 0.8 —— 排除 Q_hp = 1

    没有该上限时，模型对"不吸余热"（`Q_unused > 0`）与"吸上来再弃热"（`Q_dump > 0`）
    **完全中性**，构成退化（多个等价最优解）。

    ⚠️ **`heat_tolerance` 已删除**，上限改为硬等式 `[demand + ind_cap]/COP`。
    原容差是 v1.1 为"避数值困难"引入的，如今两个理由都不成立：
    分配规则已不让热泵超额交付；且容差会使 `Q_dump ≡ 0` 不成立。

    ⚠️ **入参 `Q_rec` 必须按 `IT_served` 计算**（见 waste_heat）。
    故障模式下失供越多，可用余热越少。

    **由此得到可测不变量**：正常模式下 `Q_dump(t) ≡ 0`。
    `Q_dump` 退化为故障模式与诊断用的变量。

    Args:
        Q_rec: 可回收余热（MW_th）
        cop: 热泵性能系数
        Q_mun_demand: 市政供热刚性需求（MW_th）；None 表示不施加需求侧约束
        Q_ind_capacity: 工业用热接纳能力上限（MW_th）

    Returns:
        逐时段的热泵电耗上限（MW）
    """
    source_limit = _nonnegative(Q_rec, "Q_rec") / (_cop(cop) - 1)
    if Q_mun_demand is None:
        if Q_ind_capacity is not None:
            raise ValueError("指定工业接纳上限时也须提供市政需求（可为零）")
        return source_limit
    municipal = _nonnegative(Q_mun_demand, "Q_mun_demand")
    industrial = 0 if Q_ind_capacity is None else _nonnegative(Q_ind_capacity, "Q_ind_capacity")
    return np.minimum(source_limit, (municipal + industrial) / cop)


def heat_output(P_hp: np.ndarray, cop: float) -> np.ndarray:
    """热泵总产热 Q_hp = COP * P_hp（MW_th）。"""
    return _cop(cop) * _nonnegative(P_hp, "P_hp")


# ---------------------------------------------------------------- 热源侧平衡（SPEC §2.4）

def heat_source_balance(
    Q_rec: np.ndarray, P_hp: np.ndarray, cop: float
) -> tuple[np.ndarray, np.ndarray]:
    """热源侧平衡：可回收余热分为"被热泵吸取"与"未被利用"两部分。

        Q_low_used(t) = P_hp(t) · ( COP − 1 )        [MW_th]  热泵实际吸取的低品位热
        Q_unused(t)   = Q_rec(t) − Q_low_used(t) ≥ 0 [MW_th]  未利用的余热

    约束：Q_low_used(t) ≤ Q_rec(t)

    ⚠️ **`Q_unused` 与 `Q_dump` 是两个不同的量**（这正是 S3 指出的混淆）：
        - `Q_unused`：从未被热泵提走的低品位热，去向是排入环境
        - `Q_dump`  ：热泵已经提温、但未被市政/工业接纳的热
      两者位置不同、去向不同，不可合并为同一个变量。

    `Q_unused` 不参与输出侧守恒式，也不计入碳抵扣（它没有替代任何热源）。

    ⚠️ 本模型假设制冷能耗不受余热回收影响（P_cool 由 IT 负荷与 PUE 决定）。
    这是简化，报告中必须说明。

    Returns:
        (Q_low_used, Q_unused)
    """
    available = _nonnegative(Q_rec, "Q_rec")
    used = _nonnegative(P_hp, "P_hp") * (_cop(cop) - 1)
    unused = available - used
    numerical_tol = 32 * np.finfo(float).eps * np.maximum(1, available)
    if np.any(unused < -numerical_tol):
        raise ValueError("热泵吸热超过可回收热源")
    return used, np.maximum(0, unused)


# ---------------------------------------------------------------- 输出侧平衡

def allocate_heat(
    Q_hp: np.ndarray,
    Q_mun_demand: np.ndarray,
    Q_ind_capacity: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把热泵产热分配给市政、工业与弃热。

    输出侧守恒式：

        Q_hp(t) = Q_mun_hp(t) + Q_ind(t) + Q_dump(t)

    市政交付恒等式（由备用热源兜底，正常模式）：

        Q_mun_hp(t) + Q_boiler(t) = Q_mun_demand(t)

    ⚠️ **本函数是事后核算 / 验证用的分配规则，不是 MILP 的约束**（v2.2）。

    原因：`min()/max()` 的**图不是凸集**，不能作为线性约束的可行域。
    反例（`D` 为参数、`m = min(H, D)`）：点 `(0, 0)` 与 `(2D, D)` 都在图上，
    但中点 `(D, D/2)` 不在——`min(D, D) = D ≠ D/2`。

    MILP 中的正确写法是**每时段一个二进制 `y(t)`**（SPEC §2.4，v2.3 修正界与标签）：

        y(t) ∈ {0,1}
        记 H = Q_hp(t)，D = Q_mun_demand(t)，U(t) 为 H 的常数上界
        y = 0 ⟺ 热泵产热不足（H ≤ D）→ Q_mun_hp = H
        y = 1 ⟺ 热泵产热已覆盖（H ≥ D）→ Q_mun_hp = D

        Q_mun_hp ≤ H
        Q_mun_hp ≤ D
        Q_mun_hp ≥ H − max(0, U−D) · y          M_1 = max(0, U−D)
        Q_mun_hp ≥ D − D · (1 − y)               M_2 = D

        U(t) = min( COP/(COP−1)·η_rec·IT_full , Q_mun_demand + Q_ind_capacity )
        IT_full = IT_base + gpu_capacity_plan·p_card  （满载 IT，纯数据）

    ⚠️ **M 必须是常数**（数据），不能含 IT_served 等决策表达式。
    v2.2 曾用 M = Q_hp_max(IT_served)，三处错误：界不足（M < D−M 时两分支均不可行）；
    非线性（M·y 成为双线性项）；标签反向。v2.3 修正为上述常数 M_1/M_2。
    反例见 SPEC §2.4：R-EAST 参数下，"工业优先"（`Q_mun_hp = 0.00352`、
    `Q_ind = 1.0`、`Q_boiler = 0.79648`）与"市政优先"的 `S-carbon`
    目标值**完全相同**（同为 −44.7744），因为 δ 单位热量在两者间转移时
    `Q_boiler` 与 `Q_ind` **同时**变化 δ，净效应为 `δ·(ci_ind_sub − ci_boiler) = 0`
    （默认两者都是 220）。
    **逐项检查系数符号，抓不住耦合项。**

    ⚠️ **`Q_ind` 一侧仍可靠目标压力**：其边际价值在五个策略中恒为正，
    不需要第二个二进制。**这是真正的"无需二进制"，且不依赖参数标定。**

    优先序语义（由二进制 `y(t)` 精确实现，本函数用于验证）：

        Q_mun_hp = min( Q_hp , Q_mun_demand )
        Q_ind    = min( Q_hp − Q_mun_hp , Q_ind_capacity )
        Q_dump   = Q_hp − Q_mun_hp − Q_ind              ≥ 0
        Q_boiler = max( 0 , Q_mun_demand − Q_hp )

    即"热泵产热先满足市政，剩余给工业，再剩余弃热；锅炉只补市政缺口"。

    ⚠️ **与已作废的 `z_boiler` 区分**（容易混淆，务必看清）：
    那个二进制编码的是"**是否允许烧气**"这个业务许可，因而错位（v1.7 反互补
    什么都没排除；v1.8 四式在余热过剩区使分支空置）。
    `y(t)` 编码的是**分段线性函数 `min` 本身**——这是标准做法，不是妥协。
    **"避免二进制"从来不是目标，正确性才是。**

    ⚠️ **`Q_unused ≥ 0` 不要求余热被吸尽**：热泵可低于热源侧上限运行，
    这是"余热供大于求时可弃用"的正确表达。

    **需要注意的边界（不是缺陷）**：
        - 余热供大于求且工业无人要时，规则给出"热泵只运行到覆盖市政"
          （`Q_hp = Q_mun_demand`），此时 `Q_unused > 0`——合理
        - 反例 C（热泵全停、余热全废、锅炉供全部市政）**是可行的**，
          它只是经济上最差。靠目标函数（`S-heat` / `S-cost` 含锅炉成本）区分，
          **不需要约束排除**

    **与 `hp_power_limit` 的关系**：
        热源侧上限限制热泵**能吸多少**；本规则决定**吸上来的热给谁**；
        需求侧上限 `[Q_mun_demand + Q_ind_capacity]/COP` 是**独立硬约束**，
        **不会**由本规则自动满足（见 `hp_power_limit` 的反例）。

    ⚠️ 正常调度中 Σ_t Q_dump(t) 应恒为 0（需求侧上限保证）；
    故障模式下允许非零，需在报告中分别统计。

    ⚠️ **碳抵扣口径**：只有工业供热的 `Q_ind` 计入 carbon_avoid；
    市政侧的余热收益通过锅炉出力降低体现，不再单列（避免双重计算）。

    ⚠️ **碳口径的已知简化**（须在报告中说明）：默认 `ci_boiler == ci_ind_substituted`
    时，市政与工业两条供热路径的碳排相等，因此碳核算对"热量分配给谁"**是中性的**。
    分配由经济性与本规则决定，不由碳口径决定。标定后若两者不同，需复核本结论。

    Returns:
        (Q_mun_hp, Q_ind, Q_dump)
    """
    output = _nonnegative(Q_hp, "Q_hp")
    demand = _nonnegative(Q_mun_demand, "Q_mun_demand")
    capacity = _nonnegative(Q_ind_capacity, "Q_ind_capacity")
    municipal = np.minimum(output, demand)
    industrial = np.minimum(output - municipal, capacity)
    dumped = output - municipal - industrial
    return municipal, industrial, dumped
