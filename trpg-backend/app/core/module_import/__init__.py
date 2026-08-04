"""模组导入（`exec/29`）。

这一层的东西**不属于 keeper 运行时**——它是「把一份文稿变成可玩模组」的预处理，
判据见 CLAUDE.md：**模组预处理才是这个项目真正的 agent 命题**（`validate_module.py`
就是编译器报错），而主持人回合归 workflow。

放在 `app/` 而不是 `scripts/`，因为它要被 service 层调用（`exec/29` 第 3/5 步）；
`scripts/module_probe/` 的 CLI 保留成薄委托——**逻辑搬走，接缝留下**。
"""
