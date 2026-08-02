"""全局契约：所有能力和编排层共同依赖的那几样东西。

- `decision.py` —— `KeeperDecision`，由各能力的字段片段**组装**成一份整体
  schema（LLM 只收一份，切不了片）；
- `registry.py` —— 能力注册表的**机制**（八个钩子的形状）。它是叶子，运行时
  一个 `app.*` 都不 import，否则 `contract → capabilities → contract` 成环；
- `module_loader.py` —— 结构化剧本数据的加载与建模（剧本文件 gitignore，
  不进公开仓库）；
- `catalog.py` —— 模组目录注册表（scenario_id → structured 路径）。
"""
