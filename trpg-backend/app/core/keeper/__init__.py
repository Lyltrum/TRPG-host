"""COC 守秘人 agent（feat/keeper-agent 实验分支）。

对外只暴露 `KeeperAgent`——它实现 `app.core.narration.contract.Narrator` 接口，
WS 层/协议/锁完全感知不到内部实现。v2 架构（裁决→执行→叙事两阶段回合制，
见 04-两阶段回合制架构.md）：openai-agents SDK 已不在主路径上（v1 自由
工具调用被实测证明不可靠，见 agent.py 模块 docstring）。

## 🔴 目录按**能力**切，不按技术层（exec/27 阶段 3）

原来这里写的是"L1 领域模型 / L2 状态编解码 / L3 执行 / L4 编排"那套分层。
实测推翻了它：一个守秘人功能天然横跨那四层（平均改 4.1 个文件，`agent.py`
被 90 个功能里的 46 个碰过），按层分目录只会把"改一个功能跨 4 个文件"变成
"跨 6 个目录"。现在按能力垂直切：

```
capabilities/    一个能力一个目录，**新人只读这一个**
  health/ agenda/ progression/ clue_reveal/
  world_state/ movement/ skill_check/ san_check/
    每个目录里都是同一套：schema(能说什么) · prompt(什么时候说)
    · executor(说了世界怎么变) · 各自的状态与测试

registry.py      七个钩子的形状（叶子，运行时零 app.* 依赖）
primitives/      能力都要用的规则原语：dice（掷骰/成功等级）· skills（技能 id
                 白名单）· npcs（NPC 实体寻址）
deps.py          一轮回合的运行时底座：KeeperDeps · 错误类型 · 角色卡读写
```

**加一个能力 = 新建一个目录 + 在 `capabilities/__init__` 加一行**，`agent.py`
与 `turn_executor.py` 一行都不用改。

## 剩下这些为什么留在骨架

判据一句话：**共享的状态与它的读写归 runtime，用它做裁决的字段与执行归能力。**

- `phase.py` 对局阶段——心跳、叙事长度、finished 拒收行动都在读；
- `location_state.py` / `scene_state.py` 位置——叙事分组、讨论区投递、检定护栏
  都在读；
- `pending.py` 两段式待掷队列——skill_check 与 san_check 共用的流程机制；
- `decision.py` 由各能力的字段片段组装成一份整体 schema（LLM 只收一份）；
- `subject.py` 主体与权限、`turn_policy.py` 本轮撤销哪些能力；
- `prompts.py` 只剩流程类规则（0/5/6/6b/7/11/12）与局面块骨架；
- `agent.py` / `turn_executor.py` / `heartbeat.py` 编排；
- `tools.py` **只剩两个 v1 遗留函数**（读角色卡 / 读剧本，现在只有测试在调）；
- 记忆与可见性：`fact_ledger.py`(L1) · `chapter.py`(L2) · `history.py`(L3) ·
  `leak_guard.py` · `sheet_digest.py` · `prose_discipline.py` · `module_loader.py`
  · `catalog.py`。

## 🔴 本文件故意不 re-export `KeeperAgent`

原先这里写着 `from app.core.keeper.runtime.agent import KeeperAgent`，而 `tools.py` 写
`from app.core.keeper import module_loader`——`from 包 import 子模块` 会先执行
包的 `__init__`，于是 `tools → 包 → agent → tools` 成环。当时没炸只是加载顺序
凑巧，`exec/27` 阶段 0 的架构测试第一次跑就抓到了它。

包门面看着方便，代价是**任何人 import 包内任何东西都会顺带加载那个实现**。
要 `KeeperAgent` 就写 `from app.core.keeper.runtime.agent import KeeperAgent`。
"""
