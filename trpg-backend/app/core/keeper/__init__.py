"""COC 守秘人 agent（feat/keeper-agent 实验分支）。

把 LLM 当成守秘人本人，把骰子/角色卡/剧本/世界状态做成它的工具（tool-calling
loop，基于 openai-agents SDK）。对外只暴露 `KeeperAgent`——它实现
`app.core.narrator.Narrator` 接口，WS 层/协议/锁完全感知不到 SDK 的存在。

模块划分：
- module_loader.py：结构化剧本数据的加载与 pydantic 建模（剧本文件 gitignore，
  不进公开仓库——版权归模组原作者/译者）；
- dice.py：掷骰与 COC7 成功等级判定（纯函数，独立可单测）；
- tools.py：六个工具的业务实现（普通函数）+ @function_tool 薄壳；
- prompts.py：守秘人 system prompt 组装；
- agent.py：KeeperAgent 本体（SDK 接线 + 全量 events 重放）。
"""

from app.core.keeper.agent import KeeperAgent

__all__ = ["KeeperAgent"]
