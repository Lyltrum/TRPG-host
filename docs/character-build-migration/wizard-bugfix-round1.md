# 建卡向导 bug 修复第一轮（exec/12 #3–#10）

> 2026-07-29，用户在真人实测（追书人模组）中针对 redesign-v2 建卡向导报了
> 8 条问题（`docs/keeper-design/exec/12-真人实测问题清单-追书人.md` 的
> #3–#10），本文档是 Opus 通读代码后给 Sonnet 的执行规格。#1（案件简报）
> 不在本轮范围——用户已明确要求先记录，那是游戏内主持人体验的缺口，跟
> 建卡向导无关。

## 🔴 核心发现：一个后端 bug 是 #4③/#8/#9/#10① 的共同根因

`app/service/character.py::compute_character_preview()` 调用
`compute_preview()` 时**没有传 `generation_method`/`attribute_pool_total`**：

```python
result = compute_preview(
    ruleset,
    attributes=payload.attributes,
    occupation_id=payload.occupation_id,
    skills=payload.skills,
    age=payload.age,
)
```

而 `compute_preview()` 的 `generation_method` 参数默认值是
`GENERATION_POINT_BUY`。这意味着**建卡向导里所有的实时预览请求，无论玩家
实际用的是掷点池还是服务端掷骰，后端永远按"点数购买法，预算 480"去校验
属性总和**。

`_compute()`（`coc7_rules.py:458-469`）在属性校验失败时会**整体短路**，
返回一个全零/空的退化结果：

```python
if attribute_issues:
    issues.extend(attribute_issues)
    return ComputeResult(
        derived_stats={},
        occupation_skill_points=SkillPointsBudget(budget=0, spent=0, remaining=0),
        interest_skill_points=SkillPointsBudget(budget=0, spent=0, remaining=0),
        skill_view=[],
        validation=issues,
        slot_occupied_skill_ids=[],
    )
```

所以：掷点池玩家分配的属性总和一旦不等于 480（几乎总是如此，池子总值是
195–720 之间随机的），预览就会假报 `ATTRIBUTE_POINTS_EXCEEDED`（这正是
#10 截图里"属性点总数 493 超出预算 480"的真实来源——不是校验时机问题，
是校验本身算错了），进而让**整个** `derived_stats`/两个技能点预算/
`skill_view` 全部退化成空——这精确解释了：
- #4③：平均分配后衍生属性显示 0（`derived_stats={}`）；
- #8/#9：职业/兴趣技能点预算条卡在 `0/—`、`SkillRow` 的 `cap` 恒为
  `null` 导致 +/- 实际上被禁用（`canAdd = cap !== null && ...`）、显示的
  "基础值" 退化成前端本地兜底的 `skill.base`（恰好等于 25/20/5/15/10/5/5
  这些看起来"正确"但其实是因为 `skillComputeMap` 整个是空的）。

`complete_character()` 走的是 `validate_character()`，那条路径**正确**地
传了 `generation_method=character.generation_method`，所以真正提交建卡不
受影响——只有建卡过程中的**实时预览**是错的，这也是为什么这个 bug 一直
没被发现（没有测试覆盖 `generation_method="roll_pool"` 场景下的
`compute_preview`，见验证报告）。

### 修法（Batch A 的核心）

1. `CharacterPreviewRequest`（`app/dto/character.py`）新增两个可选字段：
   ```python
   generation_method: str | None = None
   attribute_pool_total: int | None = None
   ```
   仿照已有的 `age: int | None = None` 字段风格（同一个类里已经有一个这样
   的先例，照抄注释风格）。
2. `coc7_rules.py::compute_preview()` 签名新增
   `attribute_pool_total: int | None = None` 参数（目前完全没有这个参数，
   跟 `validate_character()` 不对称），透传给 `_compute(...,
   attribute_pool_total=attribute_pool_total)`。
3. `app/service/character.py::compute_character_preview()` 改成：
   ```python
   result = compute_preview(
       ruleset,
       attributes=payload.attributes,
       occupation_id=payload.occupation_id,
       skills=payload.skills,
       age=payload.age,
       generation_method=payload.generation_method or GENERATION_POINT_BUY,
       attribute_pool_total=payload.attribute_pool_total,
   )
   ```
4. 前端 `useWizardPreview.ts` 的 `previewCharacter(...)` 调用要带上
   `generationMethod: state.generationMethod, attributePoolTotal:
   state.attributePoolTotal`（camelCase，SDK 会自动转 snake_case）。
5. 跑 `scripts/export_schema.py` + SDK `npm run codegen` 同步
   `trpg-sdk/src/generated/dto.ts`（`PreviewCharacterInput` 类型会自动带
   上新字段，`trpg-sdk/src/resources/games.ts::previewCharacter` 是泛型
   透传、不用改）。
6. 新增后端测试：`compute_preview(ruleset, ..., generation_method="roll_pool",
   attribute_pool_total=<某个≠480的合法总值>)` 不应该产生
   `ATTRIBUTE_POINTS_EXCEEDED`/`ATTRIBUTE_POOL_MISMATCH`，且
   `occupation_skill_points`/`interest_skill_points`/`skill_view` 应该是
   正常非退化的值（这条测试此前完全不存在，是这个 bug 从未被发现的原因，
   务必补上，回归价值最高）。同时补一条 `generation_method="roll"` 的
   preview 测试（同样此前完全没有覆盖，`roll` 模式下属性总和常年超过
   480，此前预览也会被误判）。

---

## #3 基本信息：性别/居住地/出生地不该有默认值

- `wizard-state.ts::createInitialWizardState()`：
  `info: { name: '', playerName: '', gender: '', residence: '', birthplace: '' }`
  （`gender`/`residence`/`birthplace` 三个从有默认值改成空字符串）。
- `ConceptStep.tsx` 的 `<select>` 加一个占位选项（值为空、`disabled`
  不强制，允许被选中即代表"必须显式选"）：
  ```tsx
  <option value="" disabled>请选择性别</option>
  <option>男</option>
  <option>女</option>
  <option>其他</option>
  ```
- `wizard-selectors.ts::stepBlockers('concept', ...)` 从
  ```ts
  return state.info.name.trim() ? [] : ['请填写角色姓名']
  ```
  改成同时校验姓名和性别：
  ```ts
  const reasons: string[] = []
  if (!state.info.name.trim()) reasons.push('请填写角色姓名')
  if (!state.info.gender) reasons.push('请选择性别')
  return reasons
  ```
- 居住地/出生地保持允许为空字符串，不加校验（用户明确说"可为空"）。
- `CharacterWizardPage.tsx::buildInitialState()` 从已有角色水合时依旧读
  `existing.info.gender` 等字段——老角色卡这些字段本来就有值，不受影响。

---

## #4 属性与幸运

### ① 只保留掷点池，废弃点数购买/服务端掷骰（新建角色的默认与唯一入口）

- `createInitialWizardState()`：`generationMethod: 'roll_pool'`（原来是
  `'pointbuy'`）。
- `AttributesStep.tsx`：删除"生成方式"三按钮网格（点数购买/服务端掷骰/
  掷点池），删除 `doRollAttributes`/`handlePickMethod`/`confirmReroll`/
  `confirmRerollNow`/`hasRolledOnce` 这套重掷确认流程（连同不再使用的
  `rollAttributes` import）。替换成：
  - `state.attributePoolTotal == null` 时：显示一个醒目的主按钮"🎲 掷骰
    生成属性点"（调 `doRollPool`），不提供任何其它入口。
  - 掷完之后（`attributePoolTotal != null`）：该按钮消失或禁用（**不提供
    重掷**，见④），下面正常显示掷骰明细 + PoolBar + 八项属性分配 UI
    （这部分已经是现成的、不用大改）。
  - **兼容旧角色卡**：如果水合进来的 `existingCharacter.generationMethod`
    不是 `'roll_pool'`（历史上用点数购买/服务端掷骰建过的卡），不要强迫
    转成掷点池、不要清空其属性——保留手动分配 UI 可编辑（`attrEditable`
    的既有判断已经是 `generationMethod !== 'roll'` 不用改），只是同样不
    提供"重新选生成方式"的按钮组；这类卡片依旧不提供重掷入口。这是唯一
    需要你自己拿主意的兼容分支，保持简单，不要为了这个分支去改动别的
    通用逻辑。

### ② 单项属性卡片 UI 太大，需要压缩

`AttributeAllocCard.tsx` 当前每项纵向占用较大（图标行 + 大号 ±/输入 +
困难/极难独立一行 + 预设按钮网格 + 说明文字，`px-3.5 py-3` 外边距）。
目标：**整卡高度压缩到现在的 65% 左右，但不丢失任何信息**（当前值/困难/
极难/预设按钮/一行说明都要保留，允许更紧凑的排布，比如把"困难/极难"从
独立一行改成贴在数值右侧的小字，缩小内边距/字号/按钮直径）。具体排布你
可以自行设计，只要满足这条硬约束：8 项属性叠在一起时，用户不需要划很久
的屏幕就能看到大部分内容。

### ③ 平均分配后衍生属性不更新

这就是本文档开头那个后端 bug——按"核心发现"里的方案修好之后，
`preview.derivedStats` 会正确地跟着 `state.attr` 变化实时刷新（已有的
400ms 防抖 `useWizardPreview` 机制本身没问题，问题只在于它拿到的永远是
退化的空结果）。**不需要在 AttributesStep.tsx 里做任何额外的"手动触发
刷新"逻辑**，纯粹是数据源头修好后自然联动。

### ④ 属性掷骰、幸运掷骰都只允许一次

- 前端：见①，掷完属性池后不提供重掷按钮。`LuckCard`/`doRollLuck` 同理：
  `state.attr.LUCK != null`（已经掷过）时隐藏或禁用"掷幸运"按钮，不提供
  重掷。
- **后端也要兜底**（这个项目一贯的"服务端才是权威"原则，光前端隐藏按钮
  防不住重放请求）：
  - `app/service/character.py::roll_attribute_pool()`：进入函数后先检查
    `if character.attribute_pool_total is not None: raise AlreadyRolledError(...)`。
  - `app/service/character.py::roll_luck()`：检查
    `if "LUCK" in (character.attributes or {}): raise AlreadyRolledError(...)`。
  - 新增 `class AlreadyRolledError(ValueError): """已经掷过一次，不允许
    重掷。"""`（放在文件里其它类似 `ValueError` 子类旁边，比如
    `AttributesNotSetError` 附近）。
  - `app/core/errors.py::ErrorCode` 新增一行
    `ALREADY_ROLLED = "ALREADY_ROLLED"  # 建卡掷骰只允许一次，重复调用 → 409`
    （跟着最后一个 `ATTRIBUTES_NOT_SET` 那行的风格加在后面）。
  - `app/controller/v1/rooms.py::_ERROR_MAP` 加一行：
    `character_service.AlreadyRolledError: (ErrorCode.ALREADY_ROLLED, status.HTTP_409_CONFLICT),`
  - 两个路由处理函数（`roll_attribute_pool`/`roll_luck`）的 `except`
    元组里加上 `character_service.AlreadyRolledError`（目前这两个 handler
    的 except 元组里甚至没有 `AttributesNotSetError`，只列了
    `CharacterNotFoundError`/`RoomAuthenticationError`/
    `RoomAuthorizationError`——不用管那个，只加你需要的
    `AlreadyRolledError` 即可，不要顺手"顺便"改动其它异常类型的捕获，
    这不在本次任务范围）。
  - 补测试：对同一个 characterId 连续调用两次 `roll_attribute_pool`（或
    `roll_luck`），第二次应该抛 `AlreadyRolledError`/HTTP 409
    `ALREADY_ROLLED`。
  - 前端 `AttributesStep.tsx`/`LuckCard` 调用失败时已有的
    `friendlyErrorMessage(err, '...')` 兜底文案足够，不需要特殊处理这个
    新错误码（正常情况下按钮已经被禁用，用户碰不到这个错误，只有绕过 UI
    直接打 API 才会看到）。

---

## #5 年龄

### ① 明确"改了立即生效"的交互，消除跟"重新套用"按钮的歧义

读代码确认：`commitAge()`（输入框失焦/回车触发）**已经会自动调用
`applyNow(clamped)`**，这部分逻辑本身是对的，"改了立即生效"这句文案没有
撒谎。真正的问题是`state.ageApplied` 为真之后出现的那个"重新套用"按钮
容易让人误以为"改年龄之后还要再点一下才生效"——但它实际的用途是完全
不同的一件事：**同一个年龄重新掷一次 EDU 改进检定**（教育检定是随机的，
按钮上其实已经写了"EDU 改进检定会重掷，可能变差"）。

不要改动 `applyNow`/`commitAge` 的行为逻辑（它是对的），只改善这个按钮的
呈现，避免歧义：
- 按钮文案改得更明确，比如"🎲 同年龄重掷教育检定（结果可能变差）"，去掉
  容易被误读成"提交/套用"的"重新套用"三个字。
- 在年龄输入框下方，当 `state.ageApplied && state.ageAppliedFor ===
  state.age` 时加一行小字确认态提示（比如"✓ 已套用"），让"改了自动生效"
  这件事有一个明确的视觉反馈，不需要用户去猜有没有生效。

### ② 年龄档对照表做成可折叠

`AgeStep.tsx` 里 `<StepSection title="年龄档对照表"><AgeBandTable .../>`
包一层折叠：默认收起（比如复用项目里已有的展开/收起交互模式，没有现成
组件就用一个简单的 `useState` + 按钮切换 + 条件渲染即可，不需要引入新
依赖），标题栏可以直接点击展开/收起，展开状态不需要跨步骤记忆。

---

## #6 选职业：229 个职业需要分类，不能只截断显示前 40 个

### 数据层：`OccupationSpec` 新增 `category` 字段

调研已确认：`category` 这个字段目前**在任何地方都不存在**——不是"有数据
没接上"，是需要新写。

1. `app/dto/game.py::OccupationSpec` 新增 `category: str` 字段（紧跟
   `name` 后面，加简短注释说明这是纯 UI 导航用的分组标签，不是 COC7 规则
   本身的一部分）。
2. `app/core/coc7_content.py` 的 229 个 `OccupationSpec(...)` 构造调用
   全部补上 `category=...` 参数。

**分类taxonomy**（12 类，覆盖全部 229 个职业，每个职业归入其中一类，
不要新造更多类目、也不要合并成更少——这个数量级适合导航）：

| 分类 | 说明 / 典型职业 |
|---|---|
| 学术研究 | 教授、科学家、图书管理员、历史学家、考古学家 |
| 执法安全 | 警察、侦探、军人、保安、私家侦探 |
| 医疗保健 | 医生、护士、精神病医生、兽医 |
| 法律商业 | 律师、会计师、银行家、商人、秘书 |
| 文化艺术 | 演员、艺术家、音乐家、作家、摄影师 |
| 媒体传播 | 记者、播音员、编辑 |
| 宗教哲学 | 神职人员、玄学研究者、哲学家 |
| 户外探险 | 探险家、水手、飞行员、猎人、农民、向导 |
| 技术工艺 | 工程师、机械师、工匠、技师 |
| 政府政治 | 政治家、外交官、公务员 |
| 社会边缘 | 罪犯、赌徒、骗子、流浪汉 |
| 服务劳工 | 仆人、司机、劳工、店员 |

对每个职业按名字/描述/`skill_ids` 判断归类，遇到明显跨类的职业（比如
"记者"既是文化艺术也是媒体传播）选**最贴切的单一分类**，不要给一个职业
挂多个分类。分类完之后自查一遍：任何一个分类的数量都不应该占到总数的
40% 以上，如果某个分类明显过多，回头把边界案例挪到更精确的分类里去
（这一步不需要精确到每一个都完美，只要整体导航可用）。

### 前端：`OccupationStep.tsx` 加分类导航，去掉 40 条硬截断

- 加一排分类 tab/chip（"全部" + 12 个分类），点击筛选 `filtered` 列表
  （类似 `InterestPointsStep.tsx` 里 `SkillFilterBar` 的分类筛选交互，
  可以参考它的实现方式，不需要照抄组件）。
- 搜索框继续保留，跟分类筛选可以叠加（先按分类过滤，再按搜索词过滤）。
- 去掉 `LIST_LIMIT = 40` 的硬截断——選了具体分类后，每类职业数量应该都
  在几十个以内，可以直接全部渲染（列表本身已经有
  `max-h-[360px] overflow-y-auto` 滚动容器）。"全部" tab 且没有搜索词时
  职业数量还是 229 个，可以保留一个类似的"已显示 N / 229，试试按分类或
  搜索缩小范围"提示，但**分类选中后不再截断**——核心要求是用户必须能
  通过"分类 + 搜索"这两个维度的组合，精确找到全部 229 个职业中的任意
  一个，不能有任何职业因为排序靠后而永远不可达。

---

## #7 自选技能槽应允许留空

`wizard-selectors.ts::stepBlockers('occupation', ...)` 当前要求所有自选
槽都选中具体技能才放行"下一步"：

```ts
case 'occupation': {
  if (state.occupationId == null) return ['请先选择职业']
  const occ = ctx.ruleset?.occupations.find((o) => o.id === state.occupationId)
  const slots = occ?.choiceSlots ?? []
  const filled = slots.every((slot, i) => {
    const picks = state.slotPicks[i] ?? []
    return picks.length === slot.count && picks.every(Boolean)
  })
  return filled ? [] : ['请选满全部自选技能槽']
}
```

改成只要求选了职业，不要求槽位选满：

```ts
case 'occupation':
  return state.occupationId == null ? ['请先选择职业'] : []
```

**为什么这样改是安全的**（不需要你重新验证，`wizard-selectors.ts` 文件
开头注释已经论证过）：`slotPicks` 只是前端本地模拟选择，从不提交给
后端；后端 `_assign_choice_slots` 会在玩家给任意技能加点时自动算出最优
占槽方案，跟前端下拉框选没选无关。`occupationStarSkillIds()` 已经有
"`slotPicks` 全空时退化到 `preview.slotOccupiedSkillIds`"的兜底（见
`wizard-selectors.ts:119-124`），所以留空自选槽不会破坏 ★ 星标逻辑。

同时把 `OccupationStep.tsx` 第 102 行的说明文案：

```
lead="搜索并选择一个职业；带自选槽的职业选完槽后才算选满。"
```

改成不再暗示"必须选满"，比如：

```
lead="搜索并选择一个职业；自选槽可以先留空，后续在技能加点步骤里直接给对应技能加点即可自动占槽。"
```

---

## #8 / #9 职业技能 / 兴趣技能加点步骤

按"核心发现"修好后端 bug 后，这两步的预算条和基础值应该会自然恢复正常
更新——不需要为此单独改 `OccupationPointsStep.tsx`/
`InterestPointsStep.tsx` 的核心逻辑。

**但需要补一层防御性 UX**（即使以后再出现类似的后端异常情况，用户也不该
看到"莫名其妙卡在 0"而不知道为什么）：这两个 step 组件目前完全没有接收/
展示 `previewError`（对比 `AttributesStep.tsx` 已经有
`previewError` prop 并渲染成红字）。给这两个组件也加上 `previewError`
prop（`CharacterWizardPage.tsx` 传入，来源同一个 `useWizardPreview` 返回
值），在 `PoolBar` 上方展示，样式跟 `AttributesStep.tsx` 里的
`previewError` 渲染一致。此外，如果 `preview` 本身是 `null`（还没拿到
任何结果，比如刚进入这一步、防抖还没跑完）或者 `preview.validation` 里
存在跟属性相关的校验问题（`code` 以 `ATTRIBUTE`/`INVALID_ATTRIBUTES`
开头，或者更简单：`skillView.length === 0` 且 `preview != null`），在
`PoolBar` 上方额外展示一行"属性还没通过校验，预算暂时无法计算"提示——
具体判断条件你可以自行设计，只要保证"这两步的预算显示为 0/—" 这种情况
永远伴随一个人能看懂的解释文字，不再是沉默的空白。

---

## #10 完成步骤

### ① 校验应该在每一步实时提示，不能等到最后

`preview` 对象在整个向导生命周期内（不只是 finish 步骤）持续通过
`useWizardPreview` 的 400ms 防抖更新，`preview.validation` 在任何一步都
是可用的。在 `CharacterWizardPage.tsx` 的 footer 区域（跟现有的
`blockers.length > 0` 提示同一处，`submitError` 下方）加一段**贯穿所有
步骤**的校验提示条：当 `preview?.validation` 非空且当前步骤不是
`'finish'`（finish 步骤自己已经有一整块校验展示，不需要重复）时，展示
一行简短提示，比如"⚠️ 当前存在 N 处规则校验问题，可在完成页查看详情"，
不需要在 footer 里展开全部消息（避免跟 `blockers` 提示区拥挤），点开
可选（不强制做成可点击展开，纯提示即可，除非你觉得顺手可以加）。

**核心要求**：用户不应该像 #10 截图那样一路走到最后一步才第一次看到"属性
点总数超出预算"这类信息——只要 `preview.validation` 有内容，从校验失败
的那一步开始就应该有某种可见提示，不能悄无声息地等到 Finish 页。

### ② 去掉"导出角色卡"独立界面

`FinishStep.tsx` 里的：

```tsx
<StepSection title="导出角色卡">
  <ExportPanel character={exportCharacter} preview={preview} skills={ruleset.skills} />
</StepSection>
```

整段删除（连同不再需要的 `exportCharacter` useMemo、`ExportPanel` 的
import）。理由：点击"完成创建"（`CharacterWizardPage.tsx::goNext` 在
`isLastStep` 时调用的 `useWizardSubmit.ts::handleSubmit`）已经会自动把
角色数据 `saveCharacter` + `completeCharacter` 存进后端数据库，供守秘人
agent 读取（`get_character_sheet_impl` 直接查数据库）——这个动作不依赖
用户是否碰过"导出角色卡"面板，导出面板对"让 agent 读到角色卡"这件事毫无
作用，纯粹是容易让用户误以为"不导出就没保存"的多余步骤。

删除后检查 `ExportPanel.tsx` 组件本身、`trpg-sdk` 里
`formatDicebotFull`/`formatDicebotShort`/`formatTextCard`/
`formatCharacterJson` 这四个格式化函数是否还有其它调用方（搜索整个
`trpg-frontend`/`trpg-sdk` 代码）——如果 `ExportPanel.tsx` 是唯一消费者，
把这个组件文件也一并删除（不要留橘orphan 文件）；如果这四个 SDK 格式化
函数还被别处用到（比如角色卡查看页），只删 `ExportPanel.tsx` 和
`FinishStep.tsx` 里的引用，SDK 函数本身不要动。

---

## 验证要求（不做浏览器端到端，只做以下这些）

- 后端：`pytest`、`ruff check`、`ruff format --check`、`ty check` 全绿。
- 前端：`tsc -b`、`eslint`、`build` 全绿。
- SDK：typecheck、已有单测、build 全绿。
- e2e（`trpg-frontend/../e2e`）：`npm run test:e2e` 全绿（如果这套 e2e
  覆盖了建卡流程且用到了被这次改动影响的字段/端点，确认没有回归；如果
  e2e 走的是掷点池路径，注意它可能需要更新——遇到失败先判断是"e2e 断言
  的行为本来就该随这次修复改变"还是"真的引入了回归"，不要为了让测试变绿
  而弱化断言）。
- 不需要启动浏览器做点击验证，改完直接汇报，用户会自己在浏览器里测试。
