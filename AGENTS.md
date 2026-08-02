# TRPG-master · Agent 工作记忆（Grok / Claude 共用）

> 本文件供 Grok（`AGENTS.md`）与 Claude Code（同目录亦可读 `Claude.md` 风格）
> 在本仓库内自动加载。**回答用户一律中文。** 不写任何 AI 署名进 commit/PR。

## 仓库与分支

- **路径**：`/Users/apple/Developer/work/AIDM_ALL/TRPG-master`
- **GitHub**：`Lyltrum/TRPG-master`（fork/自有实验向）
- **主实验分支**：`feat/keeper-agent`（不开 PR 到 upstream、不碰 `main`，除非用户明确要求）
- **结构**：`trpg-backend/`（FastAPI）· `trpg-frontend/`（Vite React）· `trpg-sdk/` · `e2e/` · `docs/keeper-design/` · `模组资料/`（**gitignore，版权**）

## 版权红线（硬）

- 第三方模组正文/PDF/structured **只许**在 `模组资料/`（gitignored）。
- **禁止**写入：git 跟踪文件、commit message、PR、设计文档正文、评测报告大段剧情。
- 汇报里只允许：id、计数、字段名、AI 生成叙事（非原文）。

## 本地运行（默认）

| 服务 | 地址 | 启动 |
|------|------|------|
| 后端 | `http://127.0.0.1:8000` | `cd trpg-backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| 前端 | `http://127.0.0.1:9877` | `cd trpg-frontend && npm run dev -- --host 127.0.0.1 --port 9877` |

- 配置：`trpg-backend/.env`（**已有** `DEEPSEEK_API_KEY`；勿打印完整 key）。
- **CORS**：必须同时允许 `http://localhost:9877` 与 `http://127.0.0.1:9877`（浏览器把二者当不同源）。
  漏一边会出现「网络连接失败」= OPTIONS 预检 400 / Failed to fetch。
- 前端 API 默认：`http://127.0.0.1:8000/api/v1`（`api-client.ts`）。
- 改 seed/catalog/CORS/env 后 **必须重启后端**。

## 模组对齐（2026-07-24 起）

**前端选模组 → `selectModule(scenario_id)` → 房间 `scenario_id` → Keeper 按 catalog 加载 structured。**

权威目录：`trpg-backend/app/core/keeper/contract/catalog.py`（固定 UUID，与前端 `trpg-frontend/src/config/games.ts` 的 `SCENARIO_REGISTRY.id` **必须一致**）。

| id 后缀 | 标题 | structured 文件 |
|---------|------|-----------------|
| `…0003` | 追书人 | `模组资料/追书人.structured.json` |
| `…0004` | 科比特先生 | `模组资料/科比特先生.structured.json` |
| `…0005` | 神秘渡轮 | `模组资料/神秘渡轮.structured.json` |
| `…0006` | 复足 | `模组资料/复足.structured.json` |
| `…0007` | 死者的顿足舞 | `模组资料/死者的顿足舞.structured.json` |

- 实现：`RoomAwareKeeperNarrator`（`app/core/narration/room_aware.py`）按房间解析路径；`KEEPER_MODULE_PATH` 仅兜底。
  （`core/narrator.py` 已在 `exec/27` 阶段 1 拆成 `core/narration/` 包：`contract` 叶子 + 三个实现 + `factory`。）
- 种子：`ensure_seed_content` upsert catalog 全部 scenario。
- 建房：`CreateRoomPage` 用所选 `store.sceneId`，**禁止**再写死 `modules[0]`。
- 对局标题用 `roomInfo.moduleTitle`，不要写死「惠特利旧宅」。

### 探针 vs 组装

- **探针**（裸抽取/关系）≠ 可主持；可主持 = 有 `*.structured.json`。
- 组装脚本：`trpg-backend/scripts/module_probe/assemble.py`（含机械修补：去重 id、剪悬空边、自修截断兜底）。
- 大模组整份 LLM 自修易截断；引用类错误优先机械修。

## Keeper 功能状态（摘要）

设计文档：`docs/keeper-design/`（README 有索引）。

- ✅ 两阶段回合制 + 两段式玩家掷骰 + 议程 `agenda_fired` + trigger 自由文本
- ✅ 预处理 4a/4b：组装校验、exits/contains/sub_nodes/forms/visibility_pairs
- ✅ 路线 6：对局阶段 / `ending_reached` / 心跳（development 默认开，test 关）
- ✅ 前情 API + game.start 开场仪式 + play replay 回补
- ✅ 叙事纪律硬裁 + 迷茫强制引导；检定护栏（战斗轮豁免，见 CLAUDE.md）
  ——现在在 `keeper/capabilities/skill_check/guard.py`
- ✅ **exec/14 主体视图 P0–P5.3**：`view(subject)`、事实寻址、分头叙事按受众裁
  历史/线索/本轮原话（P5.2d）。**P6 剧情 NPC 主体判定不做**（两局试玩零发生，
  代价是每次对话多一次 LLM 往返，正撞延迟痛点）
- ✅ **exec/17 技能 id 化 (A)(B) 全完成**：裁决输出 `skill_id` 白名单；模组数据
  组装期归一，五个模组未解析技能名实测 **0 条**（原 43 条）
- ✅ **exec/21 AI 玩家三层**：座位 / 合法卡 / 行动决策。有限视角，**会犯错走弯路
  是代价不是 bug，别去"修"**（一修就滑向提示机）
- ✅ **exec/22** 职业按 id 存（同名不同项的职业会错配）
- ✅ **exec/23 零基础玩家 #50–#58 全闭环**：默认 1 人 / 一键生成卡（含 LLM 写
  背景）/ 显示名换角色名 / 技能 ±5 / 掷骰广播拆两拍 / 角色卡进 KP 局面块 /
  待掷落库 + 重连补发 / 输入锁
- ✅ **exec/24 §8.1 §8.2**：待掷检定落库、世界状态自由键收口到主体 id
- ✅ **CI 覆盖本分支**（2026-08-01）：四个 workflow 的 `push.branches` 加了
  `feat/keeper-agent`——这条分支不开 PR，push 是唯一触发点
- ✅ **exec/27 架构重构五阶段全完成**（2026-08-02）：`keeper/` 按能力垂直切成
  八片 + 八个注册钩子，`agent.py` 1391→925 行。**加一片能力 = 新建一个目录 +
  在 `capabilities/__init__` 注册一行**，编排层一行不改。
  🔴 **动 keeper 代码前先读 `trpg-backend/app/core/keeper/ARCHITECTURE.md`**
  （新人入口：目录各是什么 / 加功能动哪里 / 依赖方向为什么这样，有一致性测试
  盯着不会跟代码漂）。
- ❌ 未做：`exec/24` schema v4 章节层级 / 分层注入 / `needs_entities`（等真接
  战役模组）；`exec/08` 完整 V 函数；`exec/20` 那十几条硬化；前端 UI v2
- 冒烟：`e2e/scripts/sim-human-playability.py`

## 测试与产物

- 后端：`cd trpg-backend && .venv/bin/pytest`（**跑全量**；能力测试跟能力代码
  同目录，只跑 `tests/` 会漏掉一半）
- Keeper 全链路脚本（可选）：`e2e/scripts/run-keeper-full-e2e.ts`；产物在 `e2e/artifacts/`（勿提交）
- 冒烟：`scripts/module_probe/smoke_keeper.py --module ../模组资料/….structured.json`

## Git 习惯

- conventional commits 中文；**无** `Co-Authored-By: Claude` / Generated 徽章
- 🔴 **`git push` 一律先问、等明确答复**——包括推自己的功能分支。本地 commit、
  建本地分支、跑测试都可以直接做，**推送不行**。授权不向下延伸：这一轮同意推
  不等于这条分支后续默认同意，每次重新问。
- 🔴 **不要主动催推送**。做完一轮交付就停在本地，用户想推时会自己说。
- 推之前确认 remote 是 `origin`（`Lyltrum/TRPG-master`），**不碰 `upstream`**。
- **不要** force-push / 改写历史抹版权（用户未要求时）。

## 协作注意

- 用户常用 Claude Code 与 Grok 切换：用 `/resume-claude` 可接会话；以**仓库现状 + 本 AGENTS.md** 为准，transcript 当惰性历史。
- 用户期望：**中文**、少假设、先对齐再改、可验证地跑通。
