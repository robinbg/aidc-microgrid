# outputs 目录说明
#
# 本目录存放全部实验结果、图表与日志。
#
# 建议的目录约定：
#   outputs/<region>/<arm>/<strategy>/seed<seed>/
#       trajectories_*.parquet     内层调度轨迹（列名见 SPEC §8.2）
#       task_schedule_*.parquet    任务分配
#       metrics.json               该组全部指标
#       solver_info.json           求解状态、耗时、gap、变量数
#   outputs/matrix_summary.csv     主实验汇总表（4 臂 × 5 策略 × 5 种子）
#   outputs/bo/                    BO 运行记录与 history
#   outputs/grid_search/           网格扫描结果（BO 对照组）
#   outputs/resilience/            韧性评估逐场景结果
#   outputs/sensitivity/           三组敏感性扫描结果与边界表
#   outputs/regional_mapping.csv   四画像配置推荐表
#   outputs/figures/               全部图表
#
# 交付要求（SPEC §10）：
#   导出的每一份表格与图表都必须带"仿真结果"标注，
#   且报告中必须固定 SLA 定义、关键负荷定义、降级策略定义（见 report/tables.py）。
