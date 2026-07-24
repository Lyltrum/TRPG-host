# keeper agent 设计文档（branch-local）

> `feat/keeper-agent` 分支专用的设计沉淀。这是"完整设计的实证"——代码在
> `trpg-backend/app/core/keeper/`，**设计判断**在这里。
>
> ⚠️ branch-local：这个目录只活在本实验分支。如果以后回主分支正式推进
> agent，这些设计要重新走团队的架构文档流程（Obsidian 架构目录 + issue
> 讨论），这里的内容是彼时的输入草稿，不是权威源。
>
> 权威的设计判断同时也记在 Obsidian（跨分支/跨机器留存），这里是工程侧的
> 落地细节版。

## 文档

- [01-检定与掷骰的交互设计.md](01-检定与掷骰的交互设计.md) — A 问题：
  同步内嵌 vs 多轮闭环；线上权威掷骰 vs 线下真人上报；为什么不能工具内阻塞。
- [02-检定发起权的分层约束.md](02-检定发起权的分层约束.md) — B 问题：
  谁决定要不要掷/掷什么/什么难度；模组标注 / 即兴 / 玩家宣告三层护栏。
- [03-模组抽象契约.md](03-模组抽象契约.md) — "所有模组可抽象"到底抽象了
  什么：模组必须提供什么、agent 保证什么、N=1 的通用性缺口。
- [04-两阶段回合制架构.md](04-两阶段回合制架构.md) — v2 架构：为什么推翻
  v1 自由工具调用（四类 bug 同一病灶）；裁决→执行→叙事的回合流水线与实测对比。
- [05-导演层-心跳议程与对局阶段.md](05-导演层-心跳议程与对局阶段.md) —
  🆕 "聊天机器人感"诊断（主持人=裁判+导演，导演缺位）；三提案设计定稿：
  议程标注/世界心跳(主动发言权)/对局阶段状态机+结局侦测。

## 当前实验状态（2026-07-24 晚）

- 已实现：两阶段回合制（04）+ **两段式玩家掷骰**（01）+ 长度纪律 + 迷茫引导
  + **议程标注**（opening/agenda/`agenda_fired`）+ trigger 自由文本收缩。
- 预处理链：裸抽取/关系探针 → **4a 组装+校验** → **06 组装质量修复** →
  **4b 薄迁移**（`exits`/`contains`/`sub_nodes`/`forms`/`visibility_pairs`，
  见 [exec/07](exec/07-4b-schema长出实体图与密级配对-执行规格.md)）。
- **路线 5/6 已落地薄运行时**（见 [exec/08](exec/08-visibility运行时与导演层-执行规格.md)）：
  - 5：`visibility_revealed` 记账 + 局面注入密级状态（叙事仍全员广播，真私信未做）
  - 6：对局阶段 opening→investigation→finished、`ending_reached` 收束；
    世界心跳 ticker（**默认关** `KEEPER_HEARTBEAT_ENABLED`）
- **前后端模组对齐**：`catalog.py` + 前端 `SCENARIO_REGISTRY` + `RoomAwareKeeperNarrator`；
  建房所选 scenario 驱动 structured 加载。预设五模组（structured 在 `模组资料/`）：
  追书人 / 科比特先生 / 神秘渡轮 / 复足 / 死者的顿足舞。
- 工程记忆：仓库根 `AGENTS.md`（跨 Claude/Grok）；本地 CORS 须含 localhost **与** 127.0.0.1。
- 已验证：科比特组装冒烟 + SDK 全链路；五模组 `load_module` 通过；单元测试 keeper/narrator。
- **已知缺口**：play 页历史旁白有时不回放；`game.start` 占位开场；心跳默认关。
- **更远**：真 per-player 私信、完整 V、厚分表、心跳实机调参、前端历史/WS 硬化。
