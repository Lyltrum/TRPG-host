"""COC 守秘人 agent（feat/keeper-agent 实验分支）。

对外只暴露 `KeeperAgent`——它实现 `app.core.narration.contract.Narrator` 接口，
WS 层/协议/锁完全感知不到内部实现。v2 架构（裁决→执行→叙事两阶段回合制，
见 04-两阶段回合制架构.md）：openai-agents SDK 已不在主路径上（v1 自由
工具调用被实测证明不可靠，见 agent.py 模块 docstring）。

模块按层划分（2026-07-30 工程规范整理，详见对话记录/思考笔记）：

- **L1 领域模型**：module_loader.py（结构化剧本数据的加载与 pydantic
  建模，剧本文件 gitignore 不进公开仓库）、dice.py（掷骰/COC7 成功等级
  判定，纯函数）、decision.py（`KeeperDecision`——裁决阶段的 LLM 输出契约）。
- **L2 状态编解码**：每类 `keeper_state` 保留状态各自一个文件，统一是
  `KEY 常量 + load_*（+ format_*）` 三件套——phase.py（对局阶段）、
  visibility.py（密级配对揭开）、agenda_state.py（议程触发）、
  scene_state.py（场景指针）。写入侧统一在 tools.py。
- **L3 执行**：tools.py，keeper_state/DB 唯一允许写入的地方（`*_impl`
  函数 + write_lock）。
- **L4 编排**：turn_executor.py，把 `KeeperDecision` 哪个字段非空
  分发给 tools.py 对应的 `*_impl`（`execute_side_effects`/
  `create_pending_checks`）。
- **L5 门面**：agent.py，`KeeperAgent` 本体，串起 L1-L4 + prompts.py。
- **表现层**：prompts.py（system prompt 组装）、prose_discipline.py
  （叙事正文的代码强制纪律/事后 scrub）。
- **护栏**：check_guard.py（检定发起权第一层：模组标注优先）。
- **基础设施（非 keeper 决策逻辑，暂留在此目录）**：pending.py（待掷检定
  进程内队列）、heartbeat.py（世界心跳后台任务）、catalog.py（模组目录
  注册表）。

🔴 **本文件故意不 re-export `KeeperAgent`。**

原先这里写着 `from app.core.keeper.agent import KeeperAgent`，而 `tools.py` 写
`from app.core.keeper import dice, module_loader`——`from 包 import 子模块` 会先
执行包的 `__init__`，于是 `tools → 包 → agent → tools` 成环。现在没炸只是加载
顺序凑巧。`exec/27` 阶段 0 的架构测试第一次跑就抓到了它。

包门面看着方便，代价是**任何人 import 包内任何东西都会顺带加载那个实现**。
要 `KeeperAgent` 就写 `from app.core.keeper.agent import KeeperAgent`。
"""
