"""叙事层：抽象契约 · 各实现 · 组装工厂。

🔴 **这个 `__init__.py` 故意保持空。**

`keeper/__init__.py` 曾经在这里 `from ...agent import KeeperAgent`，结果
`from app.core.keeper.primitives import dice` 这种写法会先执行包的 `__init__`，把 agent
及其全部依赖一起拉进来，制造出 `tools → 包 → agent → tools` 的循环——
`exec/27` 阶段 0 的架构测试第一次跑就抓到了它。

包门面（在 `__init__` 里 re-export 具体实现）看着方便，代价是**任何人 import
包内任何东西都会顺带加载那个实现**。这里不重蹈覆辙：要什么就从具体模块导入。
"""
