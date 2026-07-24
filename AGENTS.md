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

权威目录：`trpg-backend/app/core/keeper/catalog.py`（固定 UUID，与前端 `trpg-frontend/src/config/games.ts` 的 `SCENARIO_REGISTRY.id` **必须一致**）。

| id 后缀 | 标题 | structured 文件 |
|---------|------|-----------------|
| `…0003` | 追书人 | `模组资料/追书人.structured.json` |
| `…0004` | 科比特先生 | `模组资料/科比特先生.structured.json` |
| `…0005` | 神秘渡轮 | `模组资料/神秘渡轮.structured.json` |
| `…0006` | 复足 | `模组资料/复足.structured.json` |
| `…0007` | 死者的顿足舞 | `模组资料/死者的顿足舞.structured.json` |

- 实现：`RoomAwareKeeperNarrator`（`app/core/narrator.py`）按房间解析路径；`KEEPER_MODULE_PATH` 仅兜底。
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
- ✅ 路线 5 薄：`visibility_revealed` 代码记账 + 局面注入（叙事仍全员广播）
- ✅ 路线 6 薄：对局阶段 / `ending_reached`；心跳默认 **关**（`KEEPER_HEARTBEAT_ENABLED`）
- ⚠️ 前端进 play 有时不回放历史旁白（历史/WS 与注入会话问题）
- ⚠️ `game.start` 仍有占位开场句，真开场常绑在首动
- ❌ 真 per-player 私信；完整 V 函数；厚实体表分表

## 测试与产物

- 后端：`cd trpg-backend && .venv/bin/pytest tests/test_keeper_*.py tests/test_narrator.py -q`
- Keeper 全链路脚本（可选）：`e2e/scripts/run-keeper-full-e2e.ts`；产物在 `e2e/artifacts/`（勿提交）
- 冒烟：`scripts/module_probe/smoke_keeper.py --module ../模组资料/….structured.json`

## Git 习惯

- conventional commits 中文；**无** `Co-Authored-By: Claude` / Generated 徽章
- 实验默认：commit + push 到 `origin/feat/keeper-agent`；**不要** force-push / 改写历史抹版权（用户未要求时）

## 协作注意

- 用户常用 Claude Code 与 Grok 切换：用 `/resume-claude` 可接会话；以**仓库现状 + 本 AGENTS.md** 为准，transcript 当惰性历史。
- 用户期望：**中文**、少假设、先对齐再改、可验证地跑通。
