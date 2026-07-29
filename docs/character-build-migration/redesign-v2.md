# 建卡向导重制设计 v2：照搬 coc-char-gen 的流程与交互

> 2026-07-29。上一轮迁移（`design.md`）只搬了 coc-char-gen 的**后端能力**（年龄调整/掷点池/
> 结构化背景故事/导出），把新字段生硬塞进了原有的 5 步向导，**没有搬它的交互设计**。
> 用户评价："只把缺点修改过来了"、"可以先暂时放弃目前的我们的前端"。
>
> 本文档是**重制设计**，不是增量修补：`CharacterPage.tsx`（1700 行单文件）整体推翻重写，
> 步骤拆分/交互模式/组件粒度照搬 coc-char-gen，视觉沿用本项目暖色主题，后端只做三处
> 加法（见 §4）。本阶段只出设计，不动任何源码。

---

## 一、诊断：现在到底哪里不行

读过两边代码后，问题不是"少了几个按钮"，是**信息架构层面的**。逐条对照：

| # | 现状 | 后果 |
|---|---|---|
| 1 | **一步塞三件事**。step 0 = 基本信息 + 职业选择；step 3 = 职业技能点 + 兴趣技能点 + 信用评级，全挤在两个 tab 里 | 每屏都在同时要求玩家做多个不相关的决定。coc-char-gen 是 8 步、每步只干一件事 |
| 2 | **职业选择器是 2 列 emoji 大卡片网格**（`CharacterPage.tsx:1109`），每张卡带 emoji + 名称 + `description` 长描述，`max-h-[320px]` 内滚动 | 后端目录已经是 **229 个职业**，而 `data/occupations.ts` 的 `OCCUPATION_ICONS` 只覆盖 id 1–30——**199 个职业显示 `❔`**；`OCCUPATION_GROUPS` 的九个分组也只列了 1–30 的 id，**选任一分组会静默隐藏 199 个职业**。这就是用户说的"展示大量的信息"却不直观 |
| 3 | **自选槽（`choiceSlots`）在前端完全没有被消费**。全仓库 grep `choiceSlots` 零命中 | 职业技能 tab 只显示固定 `skillIds`。规则书里"一项社交技能（四选一）""任意一项特长"这些槽，玩家在界面上**根本看不到**，只能在兴趣技能列表里盲目找。用户说的"某些职业能选的那些特长也给我限定好了，不会让我超出这个范围"，现在**一点都没有** |
| 4 | **属性分配只有 ±5 两个按钮 + 一个数字输入框**，没有平均分配、没有预设值 | 用户点名的"这一步还能平均分配，或者有几个固定值让你直接选"完全缺失 |
| 5 | **幸运从来没被真正掷过**。`pointbuy`/`roll_pool` 模式下 LUCK 被填成 `pointBuyRules.defaultValue`（`CharacterPage.tsx:349` 那个统一补默认值的 effect），界面上还写着"暂为默认值" | 用户点名的"之后进行幸运的投掷"缺一条真实路径。**这是后端缺口**，不是前端偷懒（见 §4-A） |
| 6 | **年龄修正表是前端硬编码的展示数据**（`CharacterPage.tsx:34` 的 `AGE_MODIFIER_TABLE`） | 违反本项目 issue #96 立的判据"规则的定义和裁决都必须在后端"。后端 `coc7_age.py` 有结构化的 `AGE_TABLE`，只是没有出口 |
| 7 | 年龄步骤是"可跳过的附加步骤"，文案写"不想套用可以直接跳过这一步" | coc-char-gen 里年龄是**自动生效**的（`ensureAgeApplied`），改年龄立刻重算，改属性立刻失效。我们做成了一个孤立的可选按钮 |
| 8 | 单文件 1700 行，34 个 `useState` | 无法局部理解、无法单测、每次改动都有踩到别处的风险 |

**结论**：不是加几个控件能修好的。重写。

---

## 二、照搬什么、不照搬什么

**照搬**（信息架构层）：8 步拆分、每步"标题 + 一句白话导语 + 少量控件"的版式、分配类界面的
"批量工具在上 / 逐项微调在下"、紧凑列表式职业选择器、限定候选集的自选槽下拉、
职业技能与兴趣技能**分成两个独立步骤**、底部固定的 上一步/下一步 + 门禁原因提示。

**不照搬**（视觉层）：coc-char-gen 是纯黑深色主题（`--bg:#0c0e12`）。本项目 `trpg-frontend`
从登录页到房间页统一是暖色羊皮纸/黄铜（`bg-card`/`text-brass-dark`/`border-border-light`），
建卡是全站的一环，**继续用现有主题**。理由：用户批评的是"展示大量的信息"和步骤组织，
不是配色；换主题会让建卡页和它前后的大厅页、房间页割裂，成本高收益负。
coc-char-gen 也不是"暗色所以好看"，是"每屏信息少所以清爽"——那部分我们要，配色不要。

也不照搬 vanilla JS 的实现手段：全量 `innerHTML = ""` 重绘、`alert()`/`confirm()` 交互、
`panel.dataset.q` 存搜索词、`document.querySelector` 找输入框回填光标。这些在 React 里
有更正常的写法。

---

## 三、关键判断：自选槽 UI 怎么在**不改后端契约**的前提下做

这是本次设计最需要交代清楚的一条。

### 两边的模型不同

- **coc-char-gen**：显式。`character.slotFills[i] = skillId`，玩家给每个槽指定技能，
  由此**直接决定**哪些技能算本职。
- **本项目后端**：隐式。客户端只提交 `skills: {技能id: 最终值}`，
  `coc7_rules.py::_assign_choice_slots` 用**最大权横贯拟阵贪心 + 增广路径**自己算出
  哪些技能占了槽（`coc7_rules.py:228`），目标是**最大化占槽点数总和**。

### 结论：前端模拟"选槽"，但不提交槽位映射，**不改后端契约**

具体做法见 §6-步骤4。前端把玩家的选择存成 `slotPicks: string[][]`（每个槽一个数组，
长度 = `slot.count`），**只用于决定"职业技能"步骤里 ★ 列表显示哪些技能**，
提交时依旧只发 `skills` 字典。

### 为什么这样安全（论证，不是感觉）

后端的分配是**最优解**，前端的选择是**一个可行解**：

1. 玩家在 UI 里的选择，按构造是一个合法的二分图匹配（每个槽只被它自己候选集里的技能占）。
2. 后端求的是同一个匹配问题上的**最大权解**，所以后端算出的"占槽点数总和" ≥ 前端 UI 隐含的那个值。
3. 而 `interest_spent = 总花费 − 占槽花费`，闸门是 `INTEREST_POINTS_EXCEEDED`（非职业技能
   花费 ≤ 兴趣预算）和 `SKILL_POINTS_EXCEEDED`（两池合计）。占槽花费只会更多 ⇒ 兴趣花费只会更少。

**所以：凡是前端 UI 认为合法的卡，后端一定也认为合法。**后端只可能比 UI 更宽松，
不可能更严。反向的差异（后端把某个点数记进职业池、而 UI 以为它在兴趣池）不会造成拒绝，
且**已经不可见**——两条预算 bar 的"已花"从 issue #114 起就直接读后端
`preview.occupationSkillPoints.spent` / `interestSkillPoints.spent`，前端不本地记账。

### 需要防的唯一一件事：文案不能承诺"你选的槽 = 系统记的账"

存在这种情形：玩家给槽 A 选了"话术"但一点没加，同时在兴趣步骤给同属候选集的"说服"加了点，
后端会把"说服"记进槽 A。此时 UI 显示的槽是"话术"，两条 bar 的分账却是按"说服"算的。
**这不是 bug，也没有任何数值后果**，但文案不能说"你选中的技能会用职业点"。
正确措辞见 §11-风险 1。

### 那要不要给后端加"接受显式槽位选择"的模式？——不要

理由三条：

1. **它会让规则变严**。现在是"存在一种可行占槽方式就合法"，改成显式后是"按你指定的这种方式
   算"，玩家一个手滑的槽位选择就能把一张规则书认可的合法卡变成非法卡。issue #114 的注释
   （`coc7_rules.py:234-253`）专门论证过这一点，是 229 职业目录那一轮硬啃出来的成果。
2. **它是纯粹的净增复杂度**。多一个可选提交字段 ⇒ 后端要同时维护"有槽映射"和"无槽映射"两条
   校验路径 ⇒ 每条都要测。而按上面的论证，显式模式能表达的合法卡是隐式模式的**真子集**。
3. **它解决不了任何真实问题**。玩家要的是"别让我选出范围外的技能"，那是**前端渲染候选集**
   的事，跟服务端要不要知道槽位映射无关。

---

## 四、后端契约核对：够用的部分，和需要补的三处

### 够用（重制**不需要**改的）

| 需求 | 现有出口 | 说明 |
|---|---|---|
| 属性列表 / 中文名 / 哪些能加点 | `RulesetRead.attributes[]`（`key`/`label`/`generation`/`pointBuy`） | 幸运 `pointBuy=false`，前端据此把它单独渲染 |
| 点数购买预算 / 单项上下限 / 默认值 | `RulesetRead.attributePointBuy`（`budget`/`minValue`/`maxValue`/`defaultValue`） | "平均分配 + 预设值"全是纯前端算术，**够用** |
| 掷点池的单项约束 | 后端硬编码 `ROLL_POOL_ATTRIBUTE_MIN/MAX/STEP = 15/90/5`，前端已有同名常量 | 是骰子公式本身的产出范围，不是可调 ruleset 数据，沿用现状 |
| 掷点池总值 | `RollAttributePoolResult{rolls[], total}` + `CharacterRead.attributePoolTotal` | 编辑旧卡也能精确恢复预算分母 |
| 年龄合法区间 | `RulesetRead.ageRange{minValue,maxValue}` = [15,89] | |
| 年龄调整执行 | `POST .../apply-age-adjustment` → `AgeAdjustmentResult`（含每次 EDU 检定明细/减值/MOV 惩罚/幸运双掷标记） | 明细足够渲染"发生了什么" |
| 职业目录 + 自选槽 | `RulesetRead.occupations[].choiceSlots[]{count, candidateSkillIds, label}` | **候选集已经带在里面**，`candidateSkillIds=null` 表示任意技能。自选槽 UI 不需要新后端字段 |
| 技能目录 + 分类 | `RulesetRead.skills[]{id,name,base,category,relatedAttr}` | `category` 可直接用于兴趣步骤的分类筛选，不用前端硬编码分组 |
| 衍生值 / 两池预算 / 每技能 base·cap / 校验报告 | `POST /systems/{id}/character/preview` → `CharacterComputeResult` | 路线乙的唯一接缝，重制后继续是唯一算力来源 |
| 服务端掷骰 | `roll-attributes`（8+1 项掷定）、`roll-attribute-pool`（掷池） | |
| 结构化背景 8 字段 | `CharacterUpdateBody.backgroundDetail` + `BACKGROUND_DETAIL_FIELDS`（前端共享常量） | 已在 `RoomPage` 消费，形状不动 |

### 需要补的三处（全部是**加法**，无破坏性变更）

#### A. `POST /rooms/{roomId}/characters/{characterId}/roll-luck` —— **必需**

**问题**：`pointbuy` / `roll_pool` 两种生成方式下，幸运从来没被掷过，前端把它填成
`attributePointBuy.defaultValue`。用户明确要求的"之后进行幸运的投掷"目前没有落点。

**请求**：无 body（同 `roll-attributes` 的鉴权方式，`X-Reconnect-Token`）。

**响应**（新 DTO `RollLuckResult`）：
```
{ "kind": "3d6x5", "dice": [4, 3, 5], "value": 60 }
```
`kind` 沿用 `AttributePoolRollView` 的命名口径；服务端把 `value` 写进
`character.attributes["LUCK"]`（不动其它属性，不动 `generation_method`）。

**为什么单开一个端点而不是塞进 `roll-attribute-pool`**：幸运独立于生成方式——点数购买法
的玩家同样需要掷幸运（`AttributeSpec.pointBuy=false` 已经说明它不参与购买），塞进掷池端点
的话点数购买法玩家就掷不到。一个端点服务三种生成方式。

**注意**：15–19 岁档的"幸运掷两次取高"由 `apply-age-adjustment` 负责（`luckRerolled` 字段已存在），
**不在这个端点里做**，否则一件事两处实现。

#### B. `CharacterComputeResult.slotOccupiedSkillIds: string[]` —— **强烈建议**

**问题**：`_compute` 内部已经算出了 `slot_occupied_ids`（`coc7_rules.py:490`）然后**丢掉了**。
前端需要它做两件事：
1. 编辑一张已保存的卡时，`slotPicks` 是纯前端状态、没有持久化，重新进入向导后 ★ 列表会退化成
   只有固定技能——玩家上次靠自选槽加的点会跑到兴趣步骤里，看起来像"我的点数被挪走了"。
2. 让"这项技能占了一个职业槽"的徽标**永远跟真实记账一致**（见 §11-风险 1）。

**替代方案（否决）**：把拟阵贪心移植到 TS 本地重算。这正是路线乙 / issue #96 明令禁止的
"前端本地做规则记账"，且这段算法是本项目最微妙的一段代码，复刻一份必然漂移。

**改动量**：`dto/character.py` 加一个字段、`coc7_rules.py::ComputeResult` 带出来、
`service` 层透传、跑一次 `npm run codegen`。**纯只读、纯加法，不影响任何校验语义。**

#### C. `RulesetRead.ageBands: AgeBandSpec[]` —— **建议，可推迟**

**问题**：`CharacterPage.tsx:34` 的 `AGE_MODIFIER_TABLE` 是硬编码在前端的规则数据，
和后端 `coc7_age.py::AGE_TABLE` 是两个源（issue #96 判据的原教旨违规：定义必须在后端）。

**形状**（直接映射 `AgeModifiers` 数据类，字段名沿用后端）：
```
AgeBandSpec { minAge, maxAge, label, eduChecks, eduFlatAdjustment,
              scdLoss, scdAffectedAttributes[], appLoss, movPenalty, luckTwice }
```

**可推迟的理由**：它纯粹是展示，不参与任何裁决（裁决在 `apply-age-adjustment` 里）。
不做的代价就是维持现状——前端留一份展示用的静态表。**若推迟，必须在那份常量上方注明
"这是后端 `coc7_age.py::AGE_TABLE` 的展示副本，改后端要同步改这里"**，不要让它看起来像
一份独立事实。

---

## 五、新的步骤拆分（8 步）

对照 coc-char-gen 的 8 步与用户点名的 9 个功能点：

| 步 | id | 标题 | coc-char-gen 对应 | 用户功能点 |
|---|---|---|---|---|
| 0 | `concept` | 基本信息 | `concept` | ① 基本信息填写 |
| 1 | `attrs` | 属性与幸运 | `attrs` | ② 掷点数池 + 分配（平均分配/预设值）③ 幸运单掷 |
| 2 | `age` | 年龄 | `age` | ④ 年龄自动套用修正 |
| 3 | `occupation` | 选职业 | `occupation` | ⑤ 职业列表式选择 + 自选槽限定候选集 |
| 4 | `occPoints` | 职业技能 | `occPoints` | ⑥ 职业技能点分配（只显示 ★） |
| 5 | `intPoints` | 兴趣技能 | `intPoints` | ⑦ 兴趣技能点分配（筛选 + 搜索） |
| 6 | `background` | 角色故事 | `background` | ⑧ 个人背景 8 字段 |
| 7 | `finish` | 完成 | `finish` | ⑨ 摘要 + 校验 + 导出 |

**9 个功能点全部有落点，没有推迟项。**

### 与现有 5 步的映射

| 现有 | 去向 |
|---|---|
| step 0 信息+职业 | 拆成 步 0（信息，**年龄输入移走**）与 步 3（职业） |
| step 1 属性 | 步 1（保留生成方式三选一，加平均分配/预设/幸运掷骰区） |
| step 2 年龄 | 步 2（从"可跳过的附加动作"改成"改年龄即生效"） |
| step 3 技能（两个 tab） | 拆成 步 4（职业技能，含信用评级卡片）与 步 5（兴趣技能） |
| step 4 摘要（装备+背景+8 字段+导出） | 拆成 步 6（装备 + 自由背景 + 8 字段）与 步 7（摘要+校验+导出） |

**年龄输入框移到步 2**：现在它在步 0，而实际生效在步 2，两处相隔三屏，玩家改了年龄不知道
要回去重新套用。合并到一处后，"改年龄 → 立即重算" 的因果关系在同一屏内可见。

---

## 六、每一步的详细交互设计

通用版式（`StepShell`）：`h2 标题` → 一句白话导语（`step-lead`）→ 分节内容（每节一个
`h3 小标题`，必要时带 `tip` 单行灰字提示）→ 底部 blockers 清单（若有）。
**每步只允许 0–2 个"批量动作"按钮**，其余都是逐项微调。

---

### 步 0 · 基本信息

**控件**：`角色姓名`（必填感但不硬拦）、`玩家名`、`性别`（下拉：男/女/其他）、
`居住地`、`出生地`。两列网格，姓名独占一行。

**默认值**：沿用现有（居住地/出生地 = 阿卡姆，性别 = 男）。

**不做**：coc-char-gen 的"故事年代"（1890s/1920s/现代）。后端 `CharacterUpdateBody` 没有
这个字段，加一个只为展示的字段不值得；年代实际由模组决定。

**门禁**：姓名非空。（coc-char-gen 是"三者填一个即可"，我们收紧到姓名——角色卡在房间里
要按名字显示，空名字下游会出现"未命名角色"。）

**接口**：无。纯本地状态。

---

### 步 1 · 属性与幸运

三节：① 生成方式 → ② 分配 → ③ 幸运。

#### ① 生成方式（三选一，横排三个按钮，沿用现有样式）

| 按钮 | 行为 | 接口 |
|---|---|---|
| 点数购买 | 切回本地分配，预算 = `ruleset.attributePointBuy.budget` | 无（PATCH 时带 `generationMethod:'pointbuy'`） |
| 服务端掷骰 | 8+1 项直接掷定，**属性区变只读** | `POST .../roll-attributes` |
| 掷点池 | 掷出总点数池，玩家手动分配 | `POST .../roll-attribute-pool` |

**掷骰/掷点池按钮需要先有草稿**（两个端点都作用于已存在的 `characterId`）。
沿用现有做法：`roomId` 缺失时禁用并给提示。**若 `characterId` 尚不存在，点击时先
`createCharacterDraft` 再调掷骰端点**——这一步现在就是这么做的，保留。

**重掷确认**：已经掷过一次再点，弹确认（不是 `window.confirm`，用页面内的确认条），
文案："重新掷点会清空已分配的属性，并作废已套用的年龄调整。" 确认后：
清空八维分配、清 `poolRolls`、`ageApplied=false`、`ageResult=null`。

**掷点明细**：掷点池模式下展示 8 条 chip：`第1次·三骰 4+3+5→60`（`kind`/`dice`/`value`
直接来自 `RollAttributePoolResult.rolls`），上方一行大字 `可分配属性点：457`。

#### ② 分配

**预算条**（置顶，粘性）：`已用 340 / 共 457 · 还剩 117`。剩余为负时整条变红。

**两种模式的规则差异（必须精确实现，后端校验口径不同）**：

| | 点数购买 | 掷点池 | 服务端掷骰 |
|---|---|---|---|
| 单项范围 | `[attributePointBuy.minValue, maxValue]` = [10,90] | [15,90] | 不可编辑 |
| 步长 | 1（`±1` 和 `±5` 都给） | 5（**只给 `±5`**，后端会校验 `% 5 != 0`） | — |
| 总和 | `≤ budget`（**允许有剩余**） | **必须精确等于 `total`** | — |
| 门禁文案 | 剩余 < 0 → "多花了 N 点" | 剩余 ≠ 0 → "还剩 N 点没分完" / "多花了 N 点" | 无 |

**批量工具行**（两个按钮 + 一行提示）：

- **`平均分配`**：算法（照搬 `evenDistributePool`，本项目里总能精确分完）：
  1. `base = clamp(floor(total / 8 / 5) * 5, min, max)`，八项都设成 `base`；
  2. `remain = total - base*8`，按属性固定顺序轮转 `+5`（跳过已到 `max` 的项），直到 `remain < 5`；
  3. 点数购买模式下 `total` 取 `budget`（480/8 = 60，正好整除）；掷点池模式下 `total` 是
     5 的倍数且落在 [195,720]，八项 [15,90] 一定能精确分完，**不会出现除不尽**。
  4. **一次 `dispatch` 写完全部八项**，不要循环调八次单项 action——否则会触发八次 preview 请求。
- **`清空`**：八项归 `min`（掷点池模式）或 `defaultValue`（点数购买模式）。

- 提示文案："可以先「平均分配」，再用 +/− 按角色想法微调。"

**属性卡**（每项一张，纵向列表，沿用现有暖色卡片样式）：

```
┌─────────────────────────────────────────┐
│ 🏋 力量 STR                        加点 │   ← 图标(现有 ATTR_ICONS) + label + key
│                    60                   │   ← 大号数字（可直接编辑的输入框）
│           困难 30 · 极难 12             │   ← 半值/五分之一值（纯展示算术）
│   [−5] [−] ────────── [+] [+5]          │   ← 点数购买给 ±1；掷点池只给 ±5
│   [40] [50] [60] [70]                   │   ← 预设快捷键（用户点名的"固定值直接选"）
│   影响近战伤害                          │   ← 一行白话说明（照搬 coc-char-gen 的 meaning 表）
└─────────────────────────────────────────┘
```

- **预设按钮 40/50/60/70**：点击直接把该项设为该值。禁用条件（照搬 coc-char-gen）：
  `其余七项之和 + q > 预算总额` 时禁用。掷点池模式下四个值都是 5 的倍数且 ≥15，天然合法。
- **数字输入框**：保留现有的"字符串镜像 + `onBlur` 提交"模式（`attrInputs`），
  失焦时夹到 `[min,max]`，掷点池模式额外向下取整到 5 的倍数。
- **改任何一项属性 ⇒ 作废年龄调整**（照搬 `beginEditAllocation`）：`ageApplied=false`，
  并在预算条下方显示黄条："改属性会取消已套用的年龄调整，之后要回第 3 步重新套用。"

#### ③ 幸运

单独一张卡（样式与属性卡一致，但没有 +/− 和预设）：

- 未掷时：大号 `—` + 按钮 `掷幸运（3d6×5）`；
- 已掷：显示数值 + 掷骰明细 chip + `重掷` 按钮；
- 一行提示："幸运单独掷，不占上面的点数。"
- **接口**：`POST .../roll-luck`（§4-A 新增）。`generationMethod === 'roll'` 时幸运已由
  `roll-attributes` 一并掷出，此处显示数值 + "已由服务端掷骰生成"，隐藏按钮。
- 15–19 岁的"幸运掷两次取高"发生在步 2 的年龄套用里，这里不做，只在提示里带一句
  "若角色 15–19 岁，第 3 步会按规则重掷两次取高"。

#### 衍生值

底部一排 pill：`HP / SAN / MP / DB / MOV`，全部读 `preview.derivedStats`，前端不算。
（现状即如此，保留。）

**门禁**：见上表；外加"幸运还没掷"。

---

### 步 2 · 年龄

**核心行为差异（相对现状）**：从"可选的附加动作"改成 coc-char-gen 那种"**改了就生效**"。

**控件**：

1. 年龄输入框（`type=number`，`min/max` 来自 `ruleset.ageRange`，失焦夹值）。
   输入过程中只更新年龄档高亮，**失焦/回车才真正套用**（照搬 coc-char-gen 的
   `onInput` 预览 / `onChange` 生效）。
2. **年龄档表格**（7 行：15–19 … 80–89，列：年龄 / 教育 / 身体·外貌 / 移动 / 其他），
   当前年龄所在行高亮。数据源见 §4-C（补了就读 `ruleset.ageBands`，没补就读前端展示副本）。
3. 一行白话小结（照搬 `plainAgeNote`）："学历有 2 次「可能变高」的机会；力气/体质/灵活一共
   减 5；长相减 5；跑得慢一点（移动−1）。"
4. 套用结果面板（`AgeAdjustmentResult` 到手后）：
   - 每次 EDU 改进检定一行：`d100 掷出 73 > EDU 65 → 成功，+1d10 = 7，EDU 65→72`
     （成功/失败都要显示，失败也是"发生了什么"的一部分）；
   - `scdLoss` / `appLoss` / `movPenalty` / `luckRerolled` 各一行；
   - 属性前后对照（`attributesBefore` → `attributesAfter`），有变化的项高亮。
5. `重新套用` 按钮：EDU 改进检定是随机的，玩家可以重掷（照搬 coc-char-gen 的
   "再算一次年龄效果"）。**必须提示这会重掷 EDU，可能变差。**

**时序（关键，现状已踩过坑，保留）**：`apply-age-adjustment` 作用于**后端已保存**的
`character.attributes`，而步 1 的分配全程只在本地。所以套用前必须先
`syncCurrentStateToBackend()`（PATCH 一次当前属性），再调端点，再用返回的
`attributesAfter` 覆盖本地 `attr`。

**幂等**：同一个年龄不重复套用（`ageAppliedFor === age` 就跳过），除非点了 `重新套用`。

**门禁**：`ageApplied === true`。（年龄是必经步骤，不再"可跳过"——MOV/EDU 都受它影响，
跳过等于默认玩了一个没有年龄的角色。）

**接口**：`POST .../apply-age-adjustment`（已存在）+ 套用前的 `PATCH`（已存在）。

---

### 步 3 · 选职业

**布局**：搜索框 → 紧凑列表 → （选中后）详情面板 → 自选槽区。

#### 搜索 + 列表

- 搜索框：按 `name` 匹配（`description` 也可选匹配，但**不在列表行里渲染 description**）。
- **删除现有的"分类"下拉**。理由：`OCCUPATION_GROUPS` 只覆盖 id 1–30，选任一分组会静默
  隐藏 199 个职业——这是个功能性错误，不是样式问题。`OccupationSpec` 目前**没有** `category`
  字段，没有正确的分组依据。（未来若后端给职业加 `category`，可以把分组加回来，本期不做。）
- **列表行**（照搬 `occItem`，两行文字，无图标无描述）：
  ```
  会计师
  EDU×4 · 信用 30–70
  ```
  选中行加暖色高亮边框。
- **限流**：默认最多渲染 40 行，超出时底部一行灰字"已显示 40 / 229，请搜索缩小范围"
  （照搬 coc-char-gen）。229 行全渲染在手机上会卡，且滚 229 行找职业本来就不是好交互。
- **删除 emoji 图标**：`OCCUPATION_ICONS` 只有 30 个，199 个显示 `❔` 比没有图标更糟。
  `data/occupations.ts` 这个文件在本次重制后**没有消费方，整份删除**。

#### 详情面板（选中后出现）

- 一行摘要：`会计师 · 下一步大约有 240 点职业技能可加 · 信用评级 30–70`
  （职业点数读 `preview.occupationSkillPoints.budget`，不本地套公式）。
- 公式展示：`技能点公式 EDU×4`。
  - **若公式是 `MAX(...)` 形式**（二选一职业）：显示
    `EDU×2 + MAX(DEX, APP)×2 —— 系统按对你有利的一项计算，当前取 APP（65）`。
    **不做下拉选择**，理由见 §10-不做清单第 1 条。
- 固定本职技能：一行 chip 列表（读 `occupation.skillIds`，转成技能名）。

#### 自选槽区（本次重制的重点新增，用户点名"限定好了不会超出范围"）

对 `occupation.choiceSlots` 逐个渲染一张 slot 卡：

```
┌──────────────────────────────────────────┐
│ 一项社交技能（任选 1）                    │  ← slot.label
│ [ — 请选择 —          ▾ ]                 │  ← count 个下拉，选项 = candidateSkillIds
└──────────────────────────────────────────┘
┌──────────────────────────────────────────┐
│ 任意一项其他个人或时代特长（任选 1）      │
│ [ 搜索并选择技能…      ▾ ]                │  ← candidateSkillIds = null ⇒ 全技能表
└──────────────────────────────────────────┘
```

- 下拉的选项**只来自 `candidateSkillIds`**（转成技能中文名显示），玩家无法选出范围外的技能
  ——这正是用户要的。`candidateSkillIds === null` 时选项是全技能表（此时给个可搜索的选择器，
  79 项下拉太长）。
- 同一职业的多个槽之间**去重**：已经被别的槽选走的技能，在其余槽的下拉里禁用（灰掉并标注
  "已被其它槽选择"）。同槽 `count > 1` 时同理。
- 固定技能不出现在任何下拉里（它们已经是本职）。
- 存进 `slotPicks: string[][]`，**不提交给后端**（§3）。

**门禁**：选了职业，且所有槽都选满。

**接口**：无新增（`ruleset` 已含 `choiceSlots`）；`preview` 照常在职业变化时刷新。

---

### 步 4 · 职业技能

**★ 技能集合 = `occupation.skillIds` ∪ flatten(`slotPicks`) ∪ `{credit-rating}`。**
（编辑已有卡、`slotPicks` 为空时，用 §4-B 的 `preview.slotOccupiedSkillIds` 补上。）

**布局**：

1. **职业点预算条**（`PoolBar`）：`工作技能点 168 / 240 · 还剩 72`，读
   `preview.occupationSkillPoints`。超支变红。
2. **信用评级卡片**（保留现状的独立卡片，它是必填且有区间约束）：
   - 显示区间 `范围 30–70`、当前值、±1/±5 按钮 + 直接输入；
   - 一行说明：**"下限 30 那部分算职业点，超出下限的部分算兴趣点"**（Chaosium 官方裁定，
     后端 `_compute` 就是这么记账的，`coc7_rules.py:532-541`）；
   - 选职业时自动初始化为 `creditMin`（现状逻辑保留，含"不覆盖玩家已改过的合法值"那段）。
3. **技能列表**：只列 ★ 集合里的技能（不含 `credit-rating`，它有独立卡片）。
   每行：`技能名` + `当前总值` + `基础 25 · 已加 15 · 困难 20 / 极难 8` + `[−5][−][val][+][+5]`
   + 可直接输入的数字框。`base`/`cap` 全部读 `preview.skillView`。
   - 占槽技能带一个小徽标 `槽`（来自 `slotPicks` / `slotOccupiedSkillIds`）。
4. 兴趣点在本步**不显示控件**，但底部给一行灰字："职业点花完后，继续加点会自动占用兴趣点数
   （COC7 允许）。" ——这是本项目后端的真实行为（`SKILL_POINTS_EXCEEDED` 查的是**总预算**，
   职业池单独超支是允许的），必须说清楚，否则玩家会以为撞墙了。

**加点闸门**（保留现状的正确做法，PR #119 review 修过）：
用**两池合计剩余**判断能不能继续加，且减去 `pendingDelta`（已落本地、preview 还没确认的净加点），
不要用单池剩余。

**门禁**：软门禁。剩余 < 0 拦住（"点数花超了"）；剩余 > 0 允许继续，但点下一步时弹一次确认
"还剩 N 点没用，确定继续？"。

**接口**：`preview`（防抖 400ms，代次守卫，现状保留）。

---

### 步 5 · 兴趣技能

**布局**：

1. **兴趣点预算条**：`兴趣点数 60 / 130 · 还剩 70`，读 `preview.interestSkillPoints`。
   导语带公式来源："你有 `INT×2 = 130` 点可以随便加。"
2. **工具条**（照搬 coc-char-gen 的 `renderIntPoints`）：
   - 筛选下拉：`全部技能` / `只看本职 ★` / `只看已加点`；
   - **分类下拉**（本项目独有的加法）：读 `ruleset.skills[].category` 动态生成，
     不硬编码分组（79 项技能纯列表太长，而 `category` 是后端已有的真实数据）；
   - 搜索框（按技能名）。
3. **技能列表**：全部技能（**含**职业技能，COC7 允许兴趣点加在职业技能上），
   本职技能排在前面并带 ★（照搬 coc-char-gen 的排序：`isOccupation` 优先，再按名称）。
   行样式与步 4 相同（复用 `SkillRow`）。
   - **不可分配的技能**（后端 `NON_ALLOCATABLE_SKILL_IDS`，即克苏鲁神话）：
     禁用 +/− 并标注"建卡阶段不可加点"。

**门禁**：同步 4（软门禁）。

**接口**：`preview`。

**关于两个步骤各自记账**：现状用了一个 `interestAlloc` 副本来区分"这个技能里有多少点是兴趣点"。
**重制后取消这个副本**——`skillAlloc` 是唯一真相（`skillId → 加了多少点`），
两池分账完全由后端 `preview.*.spent` 给出。理由：`interestAlloc` 存在的唯一原因是前端曾经
要本地分账，而 issue #114 之后本地分账已经废弃；留着它就是留一份会和后端漂移的影子状态
（它已经导致过"编辑旧卡时重建 interestAlloc"这段只跑一次、条件复杂的 effect）。
两个步骤操作的是同一个 `skillAlloc`，区别只是**列表过滤条件**和**顶部显示哪条预算条**。

---

### 步 6 · 角色故事

1. **装备与物品**：一个 textarea（逗号/换行分隔，提交时切分成 `[{name}]`）。现状逻辑保留。
2. **背景故事（自由文本）**：一个 textarea → `background`。
3. **8 个引导字段**：逐个 textarea，label/placeholder 读
   `BACKGROUND_DETAIL_FIELDS`（`data/character-model.ts` 已有的共享常量，
   `RoomPage` 也在读同一份）。
4. **备注**：一个 textarea → `notes`。

**门禁**：无（全部可空，导语写明"不会写可以先空着"）。

**接口**：无（提交时随 PATCH 一起走）。

---

### 步 7 · 完成

1. **摘要卡**：姓名 / 玩家 / 年龄 / 职业 / 八维 + 幸运一行 / `HP·SAN·MP·DB·MOV` 一行。
2. **校验区**：把 `preview.validation`（`ValidationIssueView[]`）逐条渲染成红条；
   为空时显示绿条"看起来没问题"。**这是本步的核心价值**——现在玩家要点了"完成创建"
   才知道哪里不合法。
3. **主要技能**：`skillView` 里 `current > base` 或属于 ★ 的，按值降序取前 16，一行 chip。
4. **导出面板**：格式下拉（骰娘·完整 / 骰娘·精简 / 文本卡 / JSON）+ `生成` + `复制`
   + 只读 textarea。复用 `trpg-sdk/src/format/character-export.ts` 的四个纯函数（已存在）。
5. **完成创建按钮**（footer 的下一步位置）：走现状的提交流程——
   最终 `previewCharacter` 复算 → 复用或创建 `characterId` → `saveCharacter` → `completeCharacter`
   → 写 `character-store` → `navigate('/room/ready')`。
   422 错误用 `translateCharacterValidationError` 翻译。**这一整段保留不动。**

**门禁**：`preview.validation` 非空时禁用完成按钮并指向对应步骤。

---

## 七、组件拆分与文件树

`CharacterPage.tsx`（1700 行 / 34 个 `useState`）拆成：

```
trpg-frontend/src/routes/games/trpg/
  CharacterPage.tsx                     ← 改成一行 re-export，路由不用动
  character-wizard/
    CharacterWizardPage.tsx             ← 壳：header 进度条 / 步骤分发 / footer 导航 / 门禁提示
    wizard-state.ts                     ← WizardState + reducer + Action 联合类型（纯函数，可单测）
    wizard-selectors.ts                 ← 派生：★集合 / 剩余点数 / stepBlockers / canGoNext
    wizard-steps.ts                     ← STEPS 常量（id/title/short），单一事实来源
    useWizardPreview.ts                 ← 防抖 preview + 代次守卫 + pendingDelta 清零
    useWizardHydration.ts               ← 从后端读回已有卡（保留"先算后填"顺序）
    useWizardSubmit.ts                  ← 最终复算 + save + complete + 本地缓存 + 跳转
    steps/
      ConceptStep.tsx
      AttributesStep.tsx                ← 含生成方式 / 分配 / 幸运 / 衍生值四节
      AgeStep.tsx
      OccupationStep.tsx                ← 含搜索列表 / 详情 / 自选槽
      OccupationPointsStep.tsx
      InterestPointsStep.tsx
      BackgroundStep.tsx
      FinishStep.tsx
    components/
      StepShell.tsx                     ← 标题 + 导语 + 内容 + blockers 的统一版式
      PoolBar.tsx                       ← 预算条（属性池 / 职业点 / 兴趣点三处复用）
      AttributeAllocCard.tsx            ← 单项属性卡（±/预设/输入/困难极难/说明）
      LuckCard.tsx
      AgeBandTable.tsx
      AgeAdjustmentReport.tsx           ← EDU 检定明细 / 减值 / 前后对照
      OccupationListItem.tsx
      ChoiceSlotPicker.tsx              ← 自选槽下拉（含跨槽去重）
      SkillRow.tsx                      ← 从现有文件里提取（现状 90–166 行那个）
      SkillFilterBar.tsx                ← 筛选 + 分类 + 搜索
      ExportPanel.tsx
  (删除) src/data/occupations.ts        ← 图标/分组都不再用（§6-步骤3）
```

**职责边界**（下一阶段执行者按这个照做，不要自行发挥）：

| 文件 | 允许做 | 禁止做 |
|---|---|---|
| `wizard-state.ts` | 纯 reducer，只改 state | 发请求、读 ruleset、算规则数值 |
| `wizard-selectors.ts` | 从 `(state, ruleset, preview)` 派生显示用的量、门禁清单 | **任何 COC7 规则计算**（预算/base/cap/分账一律来自 preview） |
| `use*.ts` | 网络 + 副作用编排 | 渲染 |
| `steps/*.tsx` | 渲染 + `dispatch` | 直接调 SDK、本地算规则 |
| `components/*.tsx` | 受控展示组件，props 进、回调出 | 读全局 store、发请求 |

`SkillRow` 从现有实现提取时**必须原样保留** `inputValue` 字符串镜像 + `onBlur` 提交那套
（现状注释里记着为什么），以及 PR #119 review 修过的 `maxPoints` 口径。

---

## 八、状态管理

**建议：单个 `useReducer`，不引入新 store。**

理由：向导状态是**一次性、局部、随页面卸载即弃**的（真正的持久化在后端 + `character-store`
缓存）。上 zustand 会多一层生命周期问题（换房间要清理），而 34 个散装 `useState` 的真正问题
不是"没用 store"，是**状态之间的不变量散落在各个 setter 里**（改属性要作废年龄、换职业要
重置信用和槽、重掷要清一串东西）。reducer 正好把这些不变量收进一处。

```ts
type WizardState = {
  step: number
  info: { name; playerName; gender; residence; birthplace }
  age: number
  attr: Record<string, number>          // 后端属性键
  attrInputs: Record<string, string>    // 输入框字符串镜像
  generationMethod: 'pointbuy' | 'roll' | 'roll_pool'
  attributePoolTotal: number | null
  poolRolls: AttributePoolRollView[]
  luckRoll: { kind; dice; value } | null
  ageApplied: boolean
  ageAppliedFor: number | null
  ageResult: AgeAdjustmentResult | null
  occupationId: number | null
  slotPicks: string[][]                 // 纯 UI，不提交（§3）
  skillAlloc: Record<string, number>    // 唯一真相；不再有 interestAlloc
  equipment: string; background: string; notes: string
  backgroundDetail: BackgroundDetail
  ui: { occSearch; skillSearch; skillFilter; skillCategory }
}
```

**必须由 reducer 保证的不变量**（每条都对应一个 action 分支，这是拆 reducer 的全部理由）：

1. `setAttr` / `evenDistribute` / `clearAttr` / `rollPool` / `rollAttributes`
   ⇒ `ageApplied = false; ageAppliedFor = null; ageResult = null`
2. `rollPool` / `rollAttributes` ⇒ 清空 `attr` 分配、`skillAlloc` 保留（技能点不受属性重掷影响，
   但预算会变，由 preview 重新裁决）
3. `selectOccupation` ⇒ `slotPicks` 重置为 `choiceSlots.map(s => Array(s.count).fill(''))`；
   `skillAlloc['credit-rating']` 夹到新职业区间（保留现状"不覆盖仍然合法的值"的逻辑）
4. `setAge` ⇒ `ageApplied = false`（同一年龄重复设置不触发）
5. `applyAgeSuccess(result)` ⇒ `attr = result.attributesAfter; ageApplied = true; ageAppliedFor = age`
6. `setGenerationMethod('pointbuy')` ⇒ `attributePoolTotal = null; poolRolls = []`

**异步状态**（`preview` / `previewError` / `submitting` / 各种 busy）留在 hook 里的
`useState`，不进 reducer——它们不参与上面的不变量，塞进去只会让 reducer 变成杂物袋。

`pendingDelta`（加点竞态守卫）留在 `useWizardPreview` 里，跟着 preview 的生命周期走。

---

## 九、不做清单（明确列出，避免执行者纠结）

| # | coc-char-gen 有的 | 不做的理由 |
|---|---|---|
| 1 | **职业点数"用哪项属性算"的下拉**（`renderOccupation` 里 `f.choose` 那段） | 本项目后端的公式求值器支持 `MAX(...)`，**自动取较高的一项**（issue #84 补充 A）。下拉只能让玩家选到**更差**或一样的结果，没有正收益。改成一行说明文字（§6-步骤3）。 |
| 2 | **每步末尾的"我确认"复选框**（`confirmCheck`） | footer 的"下一步"已经在做同样的门禁，复选框是重复仪式。软门禁（点数没花完仍想继续）改成点下一步时的一次性确认条。 |
| 3 | **"故事年代"字段**（1890s/1920s/现代） | 后端 `CharacterUpdateBody` 没有这个字段；年代由模组决定，不由角色卡决定。 |
| 4 | **新手引导弹窗 + 每步指引折叠块**（`guide.js` / `openGuideModal` / `appendStepGuide`） | 本项目每步已有"标题 + 一句白话导语"。再加一层引导系统是新的、需要维护的产品面，超出"重制建卡向导"的范围。若以后要做，单独立项。 |
| 5 | **`custom` 自定义本职技能槽**（`slots.js` 的 `custom` resolver，最多 8 项任意技能） | 本项目后端的 `SkillChoiceSlot` 只有 `count` + `candidateSkillIds`，`null` 已经覆盖"任意技能"。coc-char-gen 那个是它自己解析原文时的兜底类型，我们的 229 职业目录已经把原文解析成结构化的槽了（issue #114），不需要兜底。 |
| 6 | **`alert()` / `confirm()` 交互** | 用页面内的提示条/确认条。 |
| 7 | **重掷属性时的整卡快照/回滚**（`snapshot` 变量） | coc-char-gen 用它做全量重算；React 里状态本来就是不可变更新，reducer 的不变量已经覆盖"重掷要清哪些东西"。 |
| 8 | **职业分组筛选**（本项目现有的 `OCCUPATION_GROUPS`，不是 coc-char-gen 的） | 只覆盖 30/229 个职业，选任一分组会静默隐藏 199 个。`OccupationSpec` 没有 `category` 字段，没有正确的分组依据。删掉，靠搜索。后端若补 `category` 再加回来。 |
| 9 | **职业 emoji 图标**（`OCCUPATION_ICONS`） | 同上，只有 30 个，其余显示 `❔`。列表式选择器本来也不需要图标。 |

---

## 十、实施顺序与验收

建议分四批，每批可独立验证（照本项目一贯的"每批跑全套便宜验证"）：

| 批 | 内容 | 验证 |
|---|---|---|
| B1 | 后端三处加法：`roll-luck` 端点（A，必需）、`slotOccupiedSkillIds`（B）、`ageBands`（C，可选）+ Alembic 无需变更 + 跑 codegen | `pytest`、`ruff check`、`ruff format --check`、`ty check`；SDK `npm run build` |
| B2 | SDK：`CharactersResource.rollLuck`，生成类型同步 | SDK typecheck + 单测 |
| B3 | 前端骨架：`wizard-state.ts` / `wizard-selectors.ts` / `wizard-steps.ts` / 壳 + 8 个空步骤 + 门禁 | 前端 `tsc` + `eslint` + `build`；reducer 不变量建议补几条纯函数单测 |
| B4 | 前端逐步骤实现 + 删除旧 `CharacterPage` 主体与 `data/occupations.ts` | 全套 + `npm run test:e2e`（e2e 覆盖的是 SDK→后端契约，不会因前端重写而失效） |

**验收清单**（对照用户的 9 点，逐条可勾）：

1. 基本信息可填、姓名为空时"下一步"被拦；
2. 掷点池后可"平均分配"一键分完，四个预设值按钮可用，±5 生效，剩余不为 0 时被拦；
3. 幸运有独立的掷骰按钮，掷出的值来自服务端，能重掷；
4. 改年龄立即重算，套用后能看到 EDU 检定明细与前后属性对照；回步 1 改属性后年龄失效并有提示；
5. 职业列表能搜索、229 个都能搜到、选中后详情面板显示公式/信用/固定技能；自选槽下拉**只**列出候选集内的技能，跨槽不重复；
6. 职业技能步骤只列 ★ 技能 + 信用评级卡片，两条数字来自后端 preview；
7. 兴趣技能步骤能按"全部/只看本职/只看已加点 + 分类 + 搜索"筛选；
8. 8 个背景字段可填，且**在游戏内 `RoomPage` 的角色卡里能看到**（上一轮踩过的读侧缺口）；
9. 完成页显示摘要 + 校验结果 + 四种导出格式可复制；提交成功后跳 `/room/ready`。

**回归底线**：`RoomPage` 的角色卡消费端（`character-store` 里的 `info`/`attr`/`skillAlloc`/
`skillFinalValues`/`derived`/`backgroundDetail`）形状**不变**，重制只换写侧。

---

## 十一、风险与待确认

1. **"选的槽"和"记的账"可能不一致（必答，文案问题）**。
   §3 已论证这没有数值后果，但玩家会看。约束：
   - 槽徽标一律以 `preview.slotOccupiedSkillIds` 为准渲染（§4-B），不以 `slotPicks` 为准；
   - 两条预算 bar 下方固定一行灰字："职业 / 兴趣两栏是系统按**对你最有利**的方式记账的结果，
     和你在上一步选的槽可能不完全对应，不影响合法性。"
   - **绝对不要**写"你选中的技能会消耗职业点"。
   - 若 §4-B 被推迟：`slotPicks` 只作用于列表过滤，槽徽标**整个不做**（宁可不显示，
     也不显示一个可能说谎的徽标）。

2. **年龄失效的传播链**。作废条件是"任何影响属性的动作"：改单项、平均分配、清空、
   重掷、切生成方式。漏掉任何一条，玩家就会带着一份套在旧属性上的年龄修正提交。
   建议在 reducer 里把这几个 action 归到同一个 `invalidateAge()` 辅助函数里，别逐个复制。

3. **`平均分配` 与 preview 防抖**。批量写八项必须是**一次 dispatch**；如果实现成循环调
   八次单项 action，会连发八次 preview（400ms 防抖只能合并连续输入，React 批处理不保证
   跨事件合并）。

4. **点数购买允许剩余、掷点池必须精确**。这两个后端校验口径不同
   （`ATTRIBUTE_POINTS_EXCEEDED` vs `ATTRIBUTE_POOL_MISMATCH`），门禁文案和"下一步"的
   可用性也必须不同。很容易写成一套逻辑然后在掷点池模式下放行一张会被 422 拒的卡。

5. **编辑已有卡的水合顺序**。现状注释（`CharacterPage.tsx:250-263`）记着一个真实教训：
   必须"先 `await preview` 算出 `allocated`，再一次性填进表单"，中途失败就整体不水合。
   重制后这段搬进 `useWizardHydration`，**不要**顺手改成"先填属性再补技能"。

6. **`slotPicks` 不持久化**。重进向导会丢。§4-B 的 `slotOccupiedSkillIds` 是补偿方案。
   若最终决定连 B 也不做，需要接受"编辑旧卡时 ★ 列表退化成只有固定技能"，
   并在步 4 加一行说明。**不接受的替代方案：把 `slotPicks` 塞进 `backgroundDetail` 或
   `notes` 里蹭持久化**——那是拿一个语义完全不同的字段当垃圾桶。

7. **229 行列表的渲染成本**。限流 40 行 + 搜索是本期方案。若实测仍卡，再考虑虚拟滚动，
   **不要一上来就引虚拟滚动库**。

8. **`roll-luck` 与年龄双掷的边界**。15–19 岁的幸运双掷在 `apply-age-adjustment` 里
   （`luckRerolled` 字段已存在）。实现 `roll-luck` 时**不要**在里面顺手判年龄，
   否则同一条规则两处实现，会出现"先掷幸运再改年龄"和"先改年龄再掷幸运"结果不同。

9. **待人工确认（列出来，别自己拍板）**——复核阶段已裁定，供执行阶段直接采信：
   - §4-C（`ageBands` 进 ruleset）：**推迟**。纯展示、不参与裁决、随时可后补，本批不做；
     前端沿用静态表但必须按文档要求在常量上方注明"这是 `coc7_age.py::AGE_TABLE` 的
     展示副本"。
   - 步 2 年龄改成**必经**：**采纳**。MOV/EDU 都受年龄影响，"可跳过"等于默认玩一个
     没有年龄的角色，这本身就不对；改成必经是修正，不是新增摩擦。
   - 职业列表限流 40 行：**采纳，不做"加载更多"**。与 coc-char-gen 原样一致，先上线用
     真实使用情况检验，不要在没有实测数据时预先加复杂度。
