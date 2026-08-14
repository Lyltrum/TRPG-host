# 守秘人（keeper）架构：新人入口

> 这份文档只回答三个问题：**目录各是什么 / 加一个功能该动哪里 / 依赖方向为什么
> 是这样**。设计的来龙去脉在 `docs/keeper-design/`，这里不重复。

## 一、先看这一条

整个目录结构对着**一条判据**设计：

> 🔴 **一个新人能不能只读一个目录，就完成一个功能——而不必理解整条链。**

所以目录**按能力垂直切**，不按技术层。曾经的分层（L1 模型 / L2 状态 / L3 执行 /
L4 编排）被实测推翻：一个守秘人功能天然横跨那四层（平均改 4.1 个文件），按层
分目录只会把"改一个功能跨 4 个文件"变成"跨 6 个目录"。

## 二、目录

```
keeper/
├── contract/       全局契约：所有人都依赖的那几样
│   ├── decision.py     KeeperDecision —— 由各能力的字段片段组装成一份
│   ├── registry.py     能力注册表的机制（钩子的形状）· 叶子
│   ├── module_loader.py 结构化剧本的加载与建模
│   └── catalog.py      模组目录（scenario_id → structured 路径）
│
├── primitives/     规则原语：能力都要用、又不属于任何一个能力
│   ├── dice.py         1d100 · COC7 成功等级
│   ├── skills.py       技能 id 白名单
│   └── npcs.py         NPC 实体寻址（把模型写的名字解析成白名单 id）
│
├── capabilities/   🔴 一个能力一个目录，新人只读这一个
│   ├── skill_check/  san_check/  health/  movement/  madness/  presence/
│   ├── world_state/  clue_reveal/  agenda/  progression/  open_threads/
│   └── closure/      luck_spend/  cast/
│
├── runtime/        编排与共享运行时状态（能力无关）
│   ├── agent.py        KeeperAgent 门面（实现 Narrator 接口）
│   ├── turn_executor.py 两条执行路径，纯由注册表驱动
│   ├── turn_policy.py  分类与代码强制（那条 if/elif 顺序即语义）
│   ├── llm_calls.py    三次模型往返的全部旋钮
│   ├── decision_log.py 裁决留痕
│   ├── deps.py         一轮的依赖包 + 角色卡读写
│   ├── phase.py        对局阶段（共享状态）
│   ├── location_state.py / scene_state.py  谁在哪（共享状态）
│   ├── madness_state.py 谁在疯（共享状态：进入由 san_check 写、解除由 madness 写）
│   ├── pending.py      两段式待掷队列
│   └── heartbeat.py    世界心跳
│
├── memory/         三层记忆：fact_ledger(L1) · chapter(L2) · history(L3)
├── access/         view(subject)：subject.py（主体与权限）· leak_guard.py
└── narration/      给模型看的文本：situation · prompts · prose_discipline
                    · narration_hints · sheet_digest
```

## 三、加一个功能，动哪里

### 场景 A：加一种守秘人能力（最常见）

**新建一个目录 + 在 `capabilities/__init__.py` 的 `CAPABILITIES` 加一行。**
`agent.py` 与 `turn_executor.py` 一行都不用改。

照抄 `capabilities/health/` 的形状，按需实现下面这些钩子里用得上的几个
（`KeeperCapability` 的字段，`test_architecture_doc.py` 盯着这张表跟代码一致）：

| 钩子 | 你要回答的问题 | 漏了会怎样 |
|---|---|---|
| `schema` | 裁决器**能说什么**（贡献 `KeeperDecision` 的字段片段） | — |
| `field_capabilities` | 你那些字段**各需要什么权限**（跟 `schema` 是一对） | 受限主体的越权字段拦不住 |
| `prompt_blocks` | 裁决器**什么时候说**（带显式 `order` 的文本块） | 模型不知道有这个字段 |
| `executors` | 说了之后**世界怎么变** | 裁决了但什么都没发生 |
| `situations` | 让模型**看见**自己改成了什么样 | 下一轮只能从上一段散文里猜 |
| `audit` | 本轮做没做事，进日志与 `keeper.decision` 事件 | 这片能力在排查时**隐身** |
| `reserved_state_keys` | 你在 `keeper_state` 里占哪些键 | 模型一条 `state_updates` 就能覆盖你的记账 |
| `pendings` | 两段式掷骰·**发起** | — |
| `settlers` | 两段式掷骰·**结算**（掷骰与生效两半写在同一行注册里） | 找不到认领者会抛（**故意不兜底**） |
| `post_settles` | 结算之后**还要再等玩家一拍**（幸运消费） | 那一拍只能写死在骨架里 |

⚠️ **唯一需要手写第二处的是 `schema`**：还要在 `contract/decision.py` 的基类
列表里继承一次。漏了会被 `tests/test_capability_registry.py` 当场抓住——不会
静默。

🔴 **每片必做两件事：能力目录里加一份，骨架里删一份。** 第二件在阶段 3 漏过
三次，症状是同一条规则在 prompt 里出现两次。有测试守着
（`test_no_rule_or_example_line_is_emitted_twice`）。

### 场景 B：改一条叙事纪律

- 想让模型**写之前**就守规矩 → `narration/narration_hints.py`
- 想在它**写完之后**删/裁 → `narration/prose_discipline.py`

两边都有，因为两边都不够。⚠️ 这两个文件**整体都是概率性改进**：触发条件由代码
确定性判断，但模型服从与否是概率的。汇报时说"已改善（概率性）"。

### 场景 C：改"某类发言该怎么处理"

`runtime/turn_policy.py`。🔴 那条 if/elif 的**顺序就是语义**，拆错会**静默**改
行为。改之前先读 `tests/test_turn_classification_characterization.py`——27 格
矩阵把当前行为逐格钉死了。

## 四、依赖方向为什么是这样

```
capabilities/ ──→ contract/ · primitives/ · runtime/{deps,phase,location_state,pending}
      ↑                                    （共享的东西，故意给能力用）
      │
  runtime/ ──→ 一切
```

三条硬约束，**都有架构测试守着**（`tests/test_architecture.py`）：

1. **能力之间不许互相 import。** 出现共用的东西 → 下沉到 `primitives/` 或
   `runtime/deps.py`，或者承认边界切错了。
2. **能力不许 import 编排层**（`agent` / `turn_executor` / `heartbeat` /
   `turn_policy` / `decision_log`）。否则"加一个能力不改编排层"就没了着力点。
3. **`contract/registry.py` 运行时必须是叶子**（零 `app.*` 依赖）。它被所有能力
   import，碰一个就 `contract → capabilities → contract` 成环。

> 🔴 **没有测试守护的架构约束一定会退化。** 今天修好，两周后一句函数内 import
> 就能把环带回来，而且什么都不会变红。

### 什么归 runtime、什么归能力

一句话判据（阶段 3–4 反复用到）：

> **共享的状态与它的读写归 runtime，用它做裁决的字段与执行归能力。**

例：对局阶段的**值**归 `runtime/phase.py`（心跳、叙事长度、拒收行动都在读它），
而"什么时候推进阶段"（`opening_complete` / `ending_reached` 两个裁决字段 + 执行）
归 `capabilities/progression/`。位置同理。

### 能力之间真要传东西怎么办

用 `TurnFacts`（`contract/registry.py`）：上游 publish、下游 consume，顺序由各自
注册的 `order` 保证。**不要伸手读另一片能力的 decision 字段**——那种耦合没有
import，架构测试抓不到，是最坏的一种。

## 五、两条读代码时会用到的判据

- **保密靠「拿不到」，不是「请你别说」。** 判断一条保密是结构性还是纪律性，只看
  模型的上下文里还有没有那条信息。分头叙事时历史 / 线索账本 / 本轮原话三处一起
  按受众裁。
- **不要用自由文本当标识符。** 技能名、NPC 名、节点名一律走白名单 id。
  tool calling 不解决这个问题（它的参数同样是模型写的字符串），解决它的是
  enum / 白名单。

## 六、改完跑什么

```
.venv/bin/pytest          # 全量，别只跑改动那几个文件
ruff check . && ruff format --check . && ty check
cd ../e2e && npm run test:e2e
```

**改了裁决 prompt 的文本**还要多做两步：

1. dump 一份裁决 prompt 跟改动前逐字节 diff，**确认差异全部是有意的**
   （这条流程两次抓到无意的文本改动）；
2. 重录磁带 `.venv/bin/python scripts/record_keeper_tape.py --module
   tests/fixtures/keeper_module.json --out tests/tapes/keeper_minimal.json`。

纯搬运（文本没变）**不必重录**——磁带是按序回放的，穿得过 prompt 变化。
