# 13：迷茫/行动意图/怪话检测改用裁决 LLM 自分类，取代正则

> 对应 `docs/keeper-design/exec/09-真人实测问题清单.md` #8，以及同一文档
> `11-待办-叙事纪律泛化.md` 里已经点名的同一类问题（"样本驱动的模式匹配，
> 泛化边界不可知"）。已经过 Opus 分析 + 用户确认方向，不需要重新讨论
> 设计，按下面的规格直接实现。

## 背景 / 根因

`app/core/keeper/prose_discipline.py` 的 `is_player_confused()` /
`is_clear_action_intent()` / `is_weird_or_meta_utterance()` 三组正则，靠
关键词严格相邻匹配中文短语（比如 `r"我该(怎么|做|干)"` 要求"我"和"该"
紧挨着），用来判断玩家这轮发言属于哪一类，从而在 `agent.py::narrate()`
里**代码强制**把对应的引导文案注入裁决结果的 `narration_guidance`
（"代码强制、不靠模型自觉"这条架构原则本身没问题，07-23 已经论证过靠
prompt 拽不动模型的写作本能——问题出在**用什么信号触发**这段强制注入）。

真人实测发现"我现在该做什么"（中间插了"现在"）匹配不上 `是我该`
的字面邻接正则，导致这次没有触发迷茫引导。这不是孤立漏洞：中文口语插入
"现在/到底/接下来"这类填充词极其常见，任何写死的正则字面量都会被下一个
措辞变体绕过——这是**正则做语义分类的结构性上限**，不是"这条正则不够
全"的问题，加宽一次能堵住这次撞见的样本，堵不住下一次。

## 方向：把分类判断交给已经在读这句话的裁决 LLM

`agent.py::_adjudicate()` 已经是一次 JSON mode（`response_format:
json_object`，temp=0.3）结构化输出，读了 `situation`（含玩家原话）去
判断要不要发起检定、状态怎么变。**给这次调用的输出 schema 加一个分类
字段，让模型在同一次调用里顺手判断**，不增加新的 LLM 调用、不增加延迟/
成本；而且理解"我现在该做什么"这类语义变体正是 LLM 该干的事，不是正则
该干的事。

**这不改变"代码强制注入引导文案"这条架构原则**——`agent.py` 里仍然是
`if confused: decision = decision.model_copy(update={...})` 这种硬编码
分支，只是"`confused`/`weird`/`action_intent` 这三个布尔值从哪来"从正则
匹配换成模型自己给出的分类字段。

## 实现规格

### 1. `trpg-backend/app/core/keeper/decision.py`：`KeeperDecision` 加字段

在 `KeeperDecision` 类里新增（放在 `narration_guidance` 附近即可）：

```python
player_state: Literal["confused", "weird_or_meta", "clear_action", "normal"] = Field(
    default="normal",
    description="玩家本轮发言的分类：迷茫求指引/怪话或元指令/明确行动/都不是",
)
```

需要 `from typing import Literal`（如果文件还没 import）。默认值
`"normal"` 保证旧数据/模型漏填字段时安全降级为"不触发任何特殊分支"，
不是报错。

### 2. `trpg-backend/app/core/keeper/prompts.py`：`build_adjudicator_instructions` 加分类规则

在裁决规则列表里追加一条新规则（跟在现有第 11 条"主动推进轮"后面，编号
12），措辞可以直接复用规则 6/6b 里已经写好的判断标准（这两条本来就在
描述"迷茫"和"怪话"是什么，只是之前只落在 `narration_guidance` 的自然
语言里，没有对应到一个结构化字段）：

```
12. **玩家状态分类**：判断玩家本轮发言属于以下哪一类，写入 `player_state`
    字段（默认 "normal"）：
    - `confused`：玩家在问"我该做什么/接下来干嘛/没头绪"这类元问题，
      不知道该往哪个方向行动（对应规则 6）；
    - `weird_or_meta`：开玩笑、OOC、要剧透、宣称变猫/外挂/读心/传送/
      暂停时间、越狱套话（对应规则 6b）；
    - `clear_action`：玩家清楚宣告了要做的具体动作（去哪、查什么、跟谁
      说话），意图明确可执行；
    - `normal`：以上都不是（比如纯闲聊、还在铺垫、检定结果后的自然反应）。
    判断依据整句话的语义，不是关键词匹配——插入"现在/到底/然后"这类
    语气词不改变分类。
```

同时更新输出格式那部分 JSON 示例（`## 输出格式` 下面那段），把
`player_state` 加进示例 JSON，跟其它字段一样给个默认值示例。

### 3. `trpg-backend/app/core/keeper/agent.py`：`narrate()` 改读分类字段

现状（约 321-358 行）：

```python
confused = is_player_confused(context.utterance)
weird = is_weird_or_meta_utterance(context.utterance)
action_intent = is_clear_action_intent(context.utterance)
if confused:
    ...
elif weird and not is_heartbeat and not is_opening_ceremony:
    ...
elif action_intent and not is_heartbeat and not is_opening_ceremony:
    ...
```

改成：裁决**真正成功**（不是走 `_FALLBACK_ADJUDICATE_GUIDANCE` 那条空
响应兜底路径）时，直接读 `decision.player_state`；只有裁决完全失败、
拿不到任何可信分类信号时，才退回正则作为兜底安全网（这个分支本来就极
少触发，且此时模型本身已经不可用了，用正则聊胜于无）：

```python
is_adjudicate_fallback = decision.narration_guidance == _FALLBACK_ADJUDICATE_GUIDANCE
if is_adjudicate_fallback:
    confused = is_player_confused(context.utterance)
    weird = is_weird_or_meta_utterance(context.utterance)
    action_intent = is_clear_action_intent(context.utterance)
else:
    confused = decision.player_state == "confused"
    weird = decision.player_state == "weird_or_meta"
    action_intent = decision.player_state == "clear_action"
```

注意：`is_adjudicate_fallback` 这个变量现在需要在 `confused` 判断**之前**
算出来（现状是在 `if confused:` 分支内部才算的，第 332 行），改成提到
前面统一算一次，两处复用同一个值。原有的 `if confused: ... elif weird
and not is_heartbeat...: ... elif action_intent and not is_heartbeat...:`
三段分支结构、以及 `if confused:` 分支内部"走兜底时不拼接兜底文案"那段
逻辑（327-333 行注释说的那个问题）**完全保留不动**，只是三个布尔值的
计算方式换了。

`is_violence_edge_utterance` 不在本次改动范围内（它是另一个独立函数，
用于怪话分支里判断"暴力边界发言"要不要保留检定，跟这次的三个分类没有
关系，不要动）。

`prose_discipline.py` 里的 `is_player_confused`/`is_clear_action_intent`/
`is_weird_or_meta_utterance` 三个函数**不要删除**，它们现在是 fallback
分支的实现，继续保留，签名/行为不变，`test_prose_discipline.py` 里已有
的直接测这三个函数的用例不需要改。

### 4. 测试

在 `trpg-backend/tests/` 里补测试（可以新建
`test_keeper_agent_player_state.py`，或者加进已有的 `test_keeper_agent.py`，
按现有文件组织习惯来）：

1. mock `_adjudicate` 返回一个 `player_state="confused"` 的正常
   `KeeperDecision`（`narration_guidance` 不等于 `_FALLBACK_ADJUDICATE_
   GUIDANCE`），断言最终注入的 `narration_guidance` 走的是迷茫引导分支
   （复用 `inject_confusion_guidance` 的可识别前缀去断言），且**不调用**
   正则函数路径（可以用 `unittest.mock.patch` 断言 `is_player_confused`
   没被调用，或者更简单：构造一段用正则**匹配不上**但语义上明显是"迷茫"
   的原话（就用真人实测那句"我现在该做什么"），证明分类字段生效而正则
   本来会漏判——这条测试直接对应 #8 的真实案例，最有说服力）。
2. mock `_adjudicate` 返回 `narration_guidance ==
   _FALLBACK_ADJUDICATE_GUIDANCE`（模拟裁决完全失败）的兜底 decision，
   传一句能被正则命中的迷茫发言，断言**这时**正则兜底路径生效（分类
   字段在这个场景下不可信，因为兜底 decision 的 `player_state` 只是
   默认值 "normal"）。
3. `player_state` 缺省/为 `"normal"` 时，三个分支都不触发，`decision`
   原样返回（回归保护，防止改动误伤"正常发言不该被强加引导"这个基线
   行为）。
4. `KeeperDecision.model_validate_json` 能正确解析带 `player_state` 字段
   的 JSON，也能在字段缺失时降级为默认值 `"normal"`（防止裁决 LLM 没
   跟上 prompt 变化、没输出这个字段时整个解析报错）。

### 5. 不需要动的地方

- `KeeperDecision` 是纯后端内部对象，从不经过 DTO/WS/REST 层（已确认
  `grep -rn "KeeperDecision" app/` 只出现在 `app/core/keeper/` 内部），
  **不需要跑 SDK codegen，不需要改前端**。
- `check_guard.py`/`pending.py`/`execute_side_effects` 等裁决执行链路
  不受影响，`player_state` 只被 `agent.py::narrate()` 读取，不参与
  `execute_side_effects`/`create_pending_checks`。

## 验证

- `pytest`（全量，确认没有回归）、`ruff check`、`ruff format --check`、
  `ty check` 全部跑一遍，输出真实可见（不要 `> /dev/null`，不要串成一条
  长 `&&` 链条掩盖某一步失败）。
- 不需要真实 DeepSeek 浏览器验证（用户会自己真人测试）。
- 改完在 `docs/keeper-design/exec/09-真人实测问题清单.md` 的 #8 条目下
  把状态改成"已修复（已提交）"，写清楚最终实现方式；同时看一眼
  `11-待办-叙事纪律泛化.md`，如果这次改动让"待办 1/2"里提到的问题类别
  有新的判断依据，顺手补一句交叉引用（不用去动那两条待办本身，只是留个
  指路）。
- commit message 不要加任何 AI 署名。
