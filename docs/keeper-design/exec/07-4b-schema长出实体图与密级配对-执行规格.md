# 执行规格 · 4b：schema 长出实体图 / 形态 / 密级配对（A 的薄迁移）

> 设计依据：[03-模组抽象契约.md](../03-模组抽象契约.md) 中层三重投影与第七节路线第 4 步；
> 需求输入：`模组资料/科比特先生.B验证观察.md` §8 **A 类缺口**（gitignored，本规格只复述
> 结构观察，不抄模组正文）。
> 承接 [05](05-预处理管线组装与校验-执行规格.md) / [06](06-组装层修复-结局议程与内容保全-执行规格.md)。
> 分支 `feat/keeper-agent`（实验分支，不开 PR、不碰 upstream）。

## 0. 目标与边界

**一句话**：在**不整表重写**的前提下，让 `ScenarioModule` 长出三层逆投影所需的
**可寻址骨架位**，组装层能填、loader 能渲、校验能拦悬空引用——把 4a 冒烟里
「形状挤在一个 ModuleNode 上」的裂缝拉开一条缝。

**为什么是「薄 A」而不是完整三层新表**：完整「实体表 / 状态机 / 密级配对」分表
会一次性改掉 loader、tools、fixture、组装、冒烟与全部既有 JSON。4a 刚证明
**现有形状可主持**；4b 的职责是让缺口有**字段落点**，不是一夜迁仓。完整分表
留给后续若 A 类仍堵死主持时再做。

### 本期做（对应 A 类）

| A 类缺口（B 观察） | 本期字段/行为 |
|---|---|
| ① 战斗/多形态 | `ModuleNpc.forms[]` + `same_as[]` |
| ② `leads_to` 压邻接 | 拆出 `exits[]`（空间邻接）与 `contains[]`（包含）；`leads_to` **收窄语义**为情节/因果推进 |
| ③ 物件/子区域不可寻址 | `sub_nodes[]`（多子节点，可递归）；保留 `sub_node` 单字段向后兼容 |
| ④ 密级配对缺失 | 顶层 `visibility_pairs[]` + node/npc 可选 `public_text` |
| ⑦ agenda.effects 空 | **仅组装 prompt 提醒**填 effects（不改 schema） |

### 本期明确不做

- ❌ 完整分表（独立 Entity 表 vs Transition 表重写）
- ❌ 检定变门闩 / 成功信息结构化（A 类⑤，属运行时裁决纪律，不靠 schema 字段堆）
- ❌ 技能名规则表再造（A 类⑥，组装侧别名归一已够）
- ❌ per-observer / 随时间翻面的 **运行时 V**（路线第 5 步；本期只长**静态配对骨架**）
- ❌ 世界心跳 / 对局阶段状态机（路线第 6 步）
- ❌ SDK / 前端 / DTO / WS / alembic
- ❌ 重跑裸抽取/关系探针

### 兼容硬要求

- 旧 JSON（无新字段）**必须** `load_module` 成功；新字段全部 Optional / 默认空列表。
- `extra="ignore"` 保持：未知键仍忽略。
- 现有 fixture / 议程测试行为点不减。

---

## 1. 目标形状（定死）

### 1.1 `ModuleNode` 增补

```python
class ModuleNode(_LenientModel):
    id: str
    title: str
    kp_text: str
    # 可选：location | clue | item | event | … 自由短标签，内容层
    kind: str | None = None
    # 挣得/公开后可念给玩家的摘要（内容层）；绝密仍在 kp_text
    public_text: str | None = None
    checks: list[ModuleCheck] = []
    branches: list[ModuleBranch] = []
    # 向后兼容：单子节点
    sub_node: "ModuleNode | None" = None
    # 可寻址多子节点（物件、子房间、日志等）
    sub_nodes: list["ModuleNode"] = []
    # 情节/因果推进边（原 leads_to 语义收窄到这一侧）
    leads_to: list[str] = []
    # 空间邻接（房间↔房间）；引用其它 node id
    exits: list[str] = []
    # 包含（地点↔其中物件/子区域的 id）；被包含者通常也是 node/sub_node id
    contains: list[str] = []
```

语义口诀（写进 loader 注释 + 组装 schema 说明）：

- **`exits`** = 地图边（空间）
- **`contains`** = 层级边（包含）
- **`leads_to`** = 情节边（调查推进 / 因果解锁）
- 三者引用的 id 都必须能在「全部 node id 集合」（含 sub_node / sub_nodes）里找到

### 1.2 `ModuleNpc` 增补

```python
class ModuleNpcForm(_LenientModel):
    id: str
    name: str | None = None
    notes: str | None = None
    stats: dict | None = None


class ModuleNpc(_LenientModel):
    id: str
    name: str
    role: str | None = None
    kp_notes: str | None = None
    stats: dict | None = None
    # 公开侧可念摘要（与 kp_notes 分区）
    public_text: str | None = None
    # 同一实体的多形态（成体/幼体、战斗形态等）
    forms: list[ModuleNpcForm] = []
    # 其它 npc id：公开形象行 ↔ 秘密行等「同一实体」链接
    same_as: list[str] = []
```

### 1.3 顶层密级配对

```python
class VisibilityPair(_LenientModel):
    id: str
    # 引用 node id 或 npc id（骨架层可遍历）
    public_ref: str
    secret_ref: str
    note: str | None = None


class ScenarioModule(_LenientModel):
    ...
    visibility_pairs: list[VisibilityPair] = []
```

本期**不做**翻面状态机：配对只渲染进 system prompt，让裁决/叙事「知道哪两块是
同一事实的两面」。运行时记账留给路线第 5 步。

---

## 2. 逐文件改动清单

### 2.1 `app/core/keeper/module_loader.py`

1. 按 §1 增加字段与 `ModuleNpcForm` / `VisibilityPair`。
2. `node_by_id`：递归搜 `sub_node` **与** `sub_nodes`。
3. `npc_by_id`：若 form id 命中，**仍返回父 npc**（form 不是独立 npc）；可选
   新增 `form_by_id` **不要**——避免 API 膨胀；form 只在 render 里展示。
4. 渲染：
   - `render_node`：输出 kind、public_text、exits、contains、leads_to 分列；
     递归渲染 `sub_nodes`（与 sub_node 并列）。
   - `render_npc`：输出 public_text、same_as、forms（含 stats）。
   - 新增 `render_visibility_pairs`；`render_full` 在「调查节点」之前或 NPC
     之后插入 **【密级配对（绝密）】** 块（空则整块省略）。
5. 模块顶注释更新：形状已开始承担实体图/密级骨架，不再写「不是统一状态机」。

### 2.2 `scripts/module_probe/validate_module.py`

1. `_collect_node_ids` / `_iter_nodes`：纳入 `sub_nodes` 递归。
2. `normalize_module_skills`：递归 `sub_nodes`。
3. `check_refs` 扩展：
   - `leads_to` / `exits` / `contains` 目标 ∈ node_ids；
   - `sub_nodes[].id` 登记且不重复；
   - `npc.same_as` 目标 ∈ npc_ids；
   - `visibility_pairs`：`public_ref`/`secret_ref` 各命中 **node_ids ∪ npc_ids**；
     pair id 唯一；
   - forms[].id 在「该 npc 内」唯一（跨 npc 允许同名 id？→ **全局 forms id 不强制**，
     只保证单 npc 内不重复）。
4. 报告字段名可增 `ref_exits` 等，或仍归入 `refs` 一项（保持简单：全进 `refs`）。

### 2.3 `scripts/module_probe/assemble.py`

1. 更新 `TARGET_SCHEMA_DOC` 与 §1 对齐。
2. `STAGE2_NODE_SYSTEM`：强制区分 exits / contains / leads_to；大段地点应拆
   `sub_nodes` 给可寻址物件/子区，而不是只堆 `kp_text`；能写 `public_text` 就写。
3. `STAGE2_NPC_SYSTEM`：多形态进 `forms`；公开/秘密分行时用 `same_as` 互指；
   战斗数据优先进 `stats`/`forms[].stats`，不要只丢进旁路文本。
4. `STAGE2_AGENDA_SYSTEM`：有可执行后果时填 `effects[]`，不要总空列表。
5. 阶段 3 之后（或 compose 时）增加可选 **阶段 3b · 密级配对**：
   - 输入：已成形 nodes/npcs + 关系列表中含「公开/假象/真相/配对」类描述的边；
   - 输出：`visibility_pairs[]`；
   - 实现可 LLM 一次调用，也可启发式（`same_as` 双向 + 标题含 public/secret 的
     npc 对）；**至少一种**，优先小 LLM 调用 + 空列表兜底。
6. `_ensure_node_minimums`：setdefault `exits`/`contains`/`sub_nodes`/`leads_to`。
7. `repair_module` 提示补：exits/contains/leads_to/visibility_pairs 悬空引用删除或改写。

### 2.4 `scripts/module_probe/smoke_keeper.py`

加载后打印：

- exits 图、contains 边、sub_nodes 计数、forms 计数、visibility_pairs 条数；
- 保留原 leads_to 图打印。

### 2.5 测试与 fixture

`tests/fixtures/keeper_module.json`（原创迷你庄园，无第三方正文）：

- `hall`：`exits: ["cellar"]`，`contains: ["hall-footprints"]`（可把脚印做成
  sub_nodes 一条），`leads_to` 可仍指向 cellar 或只留 exits——**推荐**
  `exits: ["cellar"]`，`leads_to: []` 或保留一条情节边以示区分。
- `cellar`：`sub_nodes` 含原 `sub_node` 内容；`sub_node` 可删可留其一（测试两边
  查找都通）。
- `butler`：加一个 `forms` 示例或 `same_as` 指向可选第二 npc（可不加第二 npc，
  forms 一条即可）。
- 顶层一条 `visibility_pairs`：例如 public_ref=某公开 node、secret_ref=cellar。

`tests/test_keeper_agenda.py` 或新建 `tests/test_keeper_module_schema_4b.py`：

- load fixture 成功；
- `node_by_id` 能查到 sub_nodes 内 id；
- `render_full` 含 exits/contains/密级配对块关键字；
- 旧字段路径：仅 `sub_node` 无 `sub_nodes` 的最小 dict 仍能 validate。

### 2.6 文档

- `docs/keeper-design/README.md`：更新「当前实验状态」——4a/06 完成，4b 薄迁移
  进行中/完成；下一步候选：路线 5 V 运行时 或 6 导演层。
- 本规格文件本身进 git。

---

## 3. 验证清单

```
cd trpg-backend
.venv/bin/ruff check app/core/keeper/module_loader.py scripts/module_probe/ tests/test_keeper_*.py
.venv/bin/ruff format --check app/core/keeper/module_loader.py scripts/module_probe/
.venv/bin/ty check
.venv/bin/pytest tests/test_keeper_agenda.py tests/test_keeper_tools.py tests/test_keeper_module_schema_4b.py -q
```

可选（有 key、费 token）：用更新后的 assemble 重跑科比特，更新 gitignored 的
`B验证观察.md` 里「4b 后字段填充计数」。**无 key 时跳过**，以单元测试 + schema
校验为准。

不跑 SDK/前端 e2e。

---

## 4. 提交

可拆 1–2 个 commit，中文 conventional，**不要任何 AI 署名**，不要 push / 开 PR：

```
docs(keeper): 4b 执行规格——schema 长出实体图与密级配对骨架
feat(keeper): 4b 薄迁移——exits/contains/sub_nodes/forms/visibility_pairs
```

或合并为一个 feat commit（若改动紧耦合）。只提交 docs + `app/core/keeper/` +
`scripts/module_probe/` + `tests/`。`模组资料/` 不进 git。

---

## 5. 汇报要点（做完后对照）

1. 字段一览与语义口诀（exits / contains / leads_to）；
2. 渲染与校验各拦了什么；
3. fixture/测试覆盖点；
4. 明确留给 5/6 的：翻面记账、心跳、阶段机、检定门闩；
5. 若未重跑科比特组装：说明原因（无 key / 省成本），并写清重跑命令。

🔴 版权红线：模组正文只待 `模组资料/`；规格/代码/commit/汇报不出现第三方剧情正文。
