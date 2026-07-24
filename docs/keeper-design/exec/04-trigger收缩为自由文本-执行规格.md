# 执行规格 · agenda.trigger 收缩为自由文本（路线第 3 步）

> 设计依据：[03-模组抽象契约.md](../03-模组抽象契约.md) 第五节「强类型要付费」
> 与第七节路线第 3 步。
> 这是一次**外科手术式收缩**：把提案①里从《追书人》一个模组倒推的强类型
> `AgendaTrigger` 退回自由文本，同时让 `silence`（引擎侧概念，非模组时间轴）
> 自然消失。分支 `feat/keeper-agent`。

## 0. 为什么改（一句话，实现时写进注释）

`AgendaTrigger{type, at, seconds, note}` 逼预处理 agent 做**语义归类**——把
"第二个满月之夜"归到哪个枚举？归错就废，而枚举是从一个模组倒推的。契约原则：
**触发条件是"内容"（LLM 消费，自由文本），不是"结构"（代码消费，强类型）。**
主持人的裁决器手里有剧本全文 + 历史 + `keeper_state` 的游戏内时间，判断
"本轮是否达成触发"本来就该它做；代码去算第几晚反而脆（实测措辞一变就误触发）。

## 1. 目标形状（定死，不要自行发挥）

`AgendaEvent.trigger` 从 `AgendaTrigger` 对象 **变成 `str`**（自由文本，必填，
无默认值）——与已经是 `str | None` 的 `ModuleEnding.trigger` 同构。

```python
# 改前
class AgendaEvent(_LenientModel):
    ...
    trigger: AgendaTrigger      # {type, at, seconds, note}

# 改后
class AgendaEvent(_LenientModel):
    ...
    trigger: str               # 自由文本，如"第二个游戏内夜晚（对照 keeper_state 的游戏内时间）"
```

连带：

- **删除 `AgendaTrigger` 类**（`module_loader.py`）——它整个消失，`silence`
  作为一种 type 随之消失，无需在别处补"引擎侧 silence"（那属于未来的世界心跳，
  本期不碰）。
- **删除 `render_agenda_trigger` 函数**（`module_loader.py`）——trigger 是
  str 后没有渲染逻辑可言。所有原本 `render_agenda_trigger(event.trigger)` 的
  调用点，直接替换成 `event.trigger`。

## 2. 逐文件改动清单

### `app/core/keeper/module_loader.py`
- 删 `AgendaTrigger` 类（约 65–75 行）；
- `AgendaEvent.trigger: AgendaTrigger` → `trigger: str`；
- 删 `render_agenda_trigger` 函数（约 218–236 行）；
- `render_agenda` 里 `cond = render_agenda_trigger(event.trigger)` → `cond = event.trigger`；
- `ModuleEnding.trigger`（已是 `str | None`）**不动**。

### `app/core/keeper/prompts.py`
- 去掉 `from ... import render_agenda_trigger`（第 19 行的 import 列表里删这一项，
  保留 `ScenarioModule, render_full`）；
- `format_agenda_status` 里 `cond = render_agenda_trigger(event.trigger)` → `cond = event.trigger`；
- 裁决 prompt 规则 ⑧ 的措辞（约第 73 行）：把
  「game_night 到达对应夜晚 / manual 时机成熟」这种**引用旧枚举类型名**的话，
  改成不依赖类型名的表述，例如：
  > 局面块的「议程状态」列出尚未发生的事件及其**触发条件（自由文本描述）**；
  > 你对照 `keeper_state` 的游戏内时间与当前局面，判断某条的触发条件是否在本轮
  > 达成，达成就把它的 id 写进 agenda_fired。

### `tests/fixtures/keeper_module.json`（原创迷你剧本，非《追书人》）
- 两条 agenda 的 `"trigger": {...}` → 自由文本字符串。例如原来
  `{"type":"game_night","at":1}` → `"第一个游戏内夜晚"`；`manual` 那条 →
  一句自由文本触发描述（如"当调查员当众指认管家时"）。**保持 fixture 是原创
  内容，与《追书人》无关**（版权红线）。

### `tests/test_keeper_agenda.py`
按新形状改断言，**覆盖的行为点不要减少**：
- `test_fixture_loads_agenda_and_opening`：`night.trigger` 现在是 str，断言它的
  字符串内容（不再有 `.type`/`.at`）；
- `test_render_agenda_trigger_known_and_unknown`：这个测试整体重写——
  `render_agenda_trigger` 没了。用一个新测试替代它，证明**自由文本 trigger
  原样出现在 `render_agenda` / `render_full` / `format_agenda_status` 的输出里**
  （这是收缩后"trigger 能被渲染进 prompt"的等价保证）；
- 其它引用 `AgendaTrigger` 的 import / 断言全部相应更新；
- `test_render_full_includes_opening_and_secret_agenda` 等仍需通过（断言议程块
  含"绝密"字样、含 trigger 文本）。

### `模组资料/追书人.structured.json` 和 `模组资料/追书人.议程补标.md`（均 gitignore）
- 把三条 agenda 的 `trigger` 对象改成自由文本字符串（如"第二个游戏内夜晚，
  对照 keeper_state 的游戏内时间判断"）；补标 md 里对应说明同步更新。
- 🔴 这两个文件 gitignore，**不产生 commit**；但改完要确认《追书人》仍能
  `load_module` 成功、`len(module.agenda)==3`、每条 `trigger` 是非空 str。

## 3. 不要碰
- SDK / 前端 / DTO / WS / alembic（agenda 是 keeper 内部结构，从没进过这些）；
- `ModuleEnding.trigger`（已是 str，本就符合方向）；
- `agenda_fired` 记账、once 去重、`mark_agenda_fired_impl`、`format_agenda_status`
  的分区逻辑——**这些全部保留**，本次只改 trigger 的类型，不动议程机制。
- 世界心跳 / silence 的任何"引擎侧"实现——本期不存在，不要顺手起头。

## 4. 验证（每条输出真实可见，不要 /dev/null、不要长 && 链）

```
cd trpg-backend
.venv/bin/python -m pytest -q                # 现有全绿（agenda 相关用例按新形状改后仍全过）
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/ty check
```
```
cd e2e && npm run test:e2e                    # 26/26，本改动应零感知
```
另外单独验《追书人》能加载（脚本或 `python -c`，把结果贴出来）：
`load_module('模组资料/追书人.structured.json')` 成功、agenda 3 条、
每条 trigger 是非空 str。

SDK / 前端本期不动，不跑它们的构建、不跑 codegen。

## 5. 提交
一个 commit，中文 conventional，**任何形式的 AI 署名都不要**：
```
refactor(keeper): agenda.trigger 退回自由文本——触发条件是内容不是结构
```
只提交 `trpg-backend/` 下的代码与测试。`模组资料/` 的改动 gitignore，不进 commit。

**不要 push、不要开 PR、不要碰 upstream。**

## 6. 风格
匹配现有代码：注释讲**为什么**（把第 0 节的理由写进 `AgendaEvent.trigger` 的
注释——为什么它是 str 而不是结构化对象）；删干净，不留 `AgendaTrigger` 的
孤儿引用。
