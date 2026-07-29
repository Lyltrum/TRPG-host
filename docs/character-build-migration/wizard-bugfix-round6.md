# 建卡向导 bugfix round6：#23 掷骰明细可折叠 + #24 属性±1步进 + #25 下拉翻转 + #27 分类翻译 + #28 弹窗高度

> 对应 `docs/keeper-design/exec/12-真人实测问题清单-追书人.md` 的 #23/#24/
> #25/#27/#28。#26 已核实非 bug，不在本轮范围内。
> 用户已经拍板的两个决策点（不需要再讨论）：
> - #24：放开后端"掷点池模式下属性必须是 5 的倍数"这条校验，让 ±1 步进
>   也能用。
> - #25：下拉菜单被裁切，选"翻转方向"这个修法（空间不够就向上展开），
>   不做 portal+fixed 那个更大改动的方案。
>
> 三个批次按文件互不重叠划分，可以并行执行，互不阻塞。

## 批次 A（后端 + 前端，同一个 agent 顺序做）：#23 + #24

这两条都改 `trpg-frontend/src/routes/games/trpg/character-wizard/steps/
AttributesStep.tsx` 这同一个文件，所以放在同一个批次里顺序做，避免两个
并行 agent 同时改一个文件产生冲突。

### A1（#24）后端放开掷点池模式下属性的 5 的倍数校验

`trpg-backend/app/core/coc7_rules.py`：

- `ROLL_POOL_ATTRIBUTE_STEP = 5`（59 行附近）这个常量目前只在
  `_validate_attributes` 里做"必须是 5 的倍数"这条校验用（380-387 行：
  `elif is_roll_pool and key in point_buy_keys and value %
  ROLL_POOL_ATTRIBUTE_STEP != 0:` 报 `INVALID_ATTRIBUTES`）。这条校验的
  理由（代码注释里写的"骰子公式本身的产出范围"）已经跟用户核实过站不住
  脚——掷点池法是先把骰子汇总成一个总点数池再自由分配，汇总之后"单次
  掷骰产出是 5 的倍数"这个特征就该跟"重新分配时每项还要不要保持 5 的
  倍数"脱钩，只要总和不变，±1 调整不违反任何真实规则约束。
- **直接删掉这条校验分支**（380-387 行那个 `elif`），不要只是把
  `ROLL_POOL_ATTRIBUTE_STEP` 改成 1（那样 `% 1 != 0` 恒为 False，等价于
  没有这条校验，但留着一个恒假的判断分支和一个名不副实的常量名，不如
  直接删干净）。
- `ROLL_POOL_ATTRIBUTE_MIN`/`ROLL_POOL_ATTRIBUTE_MAX`（15/90）这两个区间
  边界**保留不动**——这条要拿掉的只是"步进必须是5的倍数"，不是"取值
  范围"，范围本身仍然合理（对应骰子公式的最小/最大产出）。
- 相应地，`ROLL_POOL_ATTRIBUTE_STEP` 这个常量如果删了校验分支后就没有
  其它地方引用了，顺手删掉常量定义本身；如果还有别的地方引用（比如前端
  展示文案），保留常量但更新上面那段模块级注释（36-40 行附近解释
  `ROLL_POOL_ATTRIBUTE_MIN/MAX` 由来的那段），去掉"都是 5 的整数倍"这个
  现在不成立的说法，如实说明只有掷骰当次汇总总值受骰子公式约束、单项
  分配值不再受 5 的倍数限制。
- `_validate_attributes` 函数开头的 docstring（303-329 行）里提到
  `ROLL_POOL_ATTRIBUTE_STEP` 的地方（314 行"且必须是
  `ROLL_POOL_ATTRIBUTE_STEP` 的倍数"）要同步删掉/改准确，不要留一段跟
  代码行为不一致的文档字符串。

**测试**：`trpg-backend/tests/test_coc7_rules.py` 里如果有专门测"掷点池
非5倍数应该报错"的用例，要么删掉要么反过来（改成断言非5倍数**不再**
报错、只要总和对得上就合法）；补一条新用例，掷点池模式下属性值分配成
非5倍数（比如某项分配 61）但总和仍然精确等于 `attribute_pool_total`，
断言 `validation == []`（不再报 `INVALID_ATTRIBUTES`）。

### A2（#24）前端 `AttributesStep.tsx` 放开 ±1 按钮

`trpg-frontend/src/routes/games/trpg/character-wizard/steps/
AttributesStep.tsx`（约 273 行 `step5Only={state.generationMethod ===
'roll_pool'}`）——这个 prop 传给 `AttributeAllocCard`，控制 ±1 按钮显示
与否（`AttributeAllocCard.tsx` 组件本身已经同时支持 ±1/±5，不需要改这个
组件）。掷点池模式不再需要隐藏 ±1，把这一行改成 `step5Only={false}`
（两种生成方法现在都该有 ±1/±5）。

如果 `AttributeAllocCard.tsx` 的 `step5Only` prop 现在只有这一处调用点
且改完后永远是 `false`，**不要**顺手把这个 prop 从组件签名里删掉——这
是"外科手术式修改"的边界：这次任务只解决"掷点池模式该不该有±1"这一件
事，组件签名的清理不在本次范围内，除非你确认这个 prop 从此再无意义
（比如以后新增生成方法还可能用到 5 步进），否则保留 prop、只改调用值，
避免顺手做超出任务范围的重构。

### A3（#23）掷骰明细改可折叠

同一个文件 `AttributesStep.tsx`，约 213-221 行：

```tsx
{state.generationMethod === 'roll_pool' && state.poolRolls.length > 0 && (
  <div className="mt-2.5 grid grid-cols-2 gap-1.5">
    {state.poolRolls.map((r, i) => (
      <div key={i} className="text-[10px] text-text-dim font-mono bg-panel rounded px-2 py-1">
        {r.kind} [{r.dice.join(',')}] = {r.value}
      </div>
    ))}
  </div>
)}
```

改成默认收起、可点击展开的折叠区：

- 加一个本地 `useState` 控制展开/收起（不需要进全局 wizard state，纯
  UI 展示状态，跟 `SkillCombobox` 里 `const [open, setOpen] =
  useState(false)` 那种局部状态用法一致）。
- **默认收起**（掷骰已经完成、结果不会变，玩家分配完属性后这块信息的
  即时价值较低，默认收起给下面的属性分配区腾地方）。
- 收起态显示一行摘要 + 展开箭头，比如"🎲 掷骰明细（点击展开）"配合
  一个 `ChevronDown`/`ChevronUp`（`lucide-react` 已经在这个文件/仓库里
  广泛使用，直接复用）图标随展开状态旋转或切换；展开态渲染原来那个
  `grid grid-cols-2` 明细网格。
- 不要改 `state.poolRolls`/`doRollPool` 这些数据/逻辑层，纯展示层加一层
  折叠壳。

**测试**：这是纯 UI 交互改动，跑 tsc/eslint/build 确认无误即可；如果想
补一个 vitest 测试点击展开/收起的行为变化也可以，不强制。

---

## 批次 B（前端 only，独立 agent）：#25 自选技能槽下拉菜单翻转方向

`trpg-frontend/src/routes/games/trpg/character-wizard/components/
ChoiceSlotPicker.tsx` 的 `SkillCombobox` 子组件（12-91 行）。

现状：菜单固定 `absolute left-0 right-0 top-full mt-1`（61 行）向下展开，
不管触发按钮下方还剩多少空间。

**目标**：触发按钮下方可视空间不够放下菜单时，自动翻转成向上展开
（`bottom-full mb-1` 而不是 `top-full mt-1`）。

**实现方式**：
- 给触发按钮（50-57 行的 `<button>`）加一个 `ref`（`useRef<HTMLButtonElement>`）。
- 打开菜单时（`open` 变为 `true` 的那一刻），用
  `buttonRef.current?.getBoundingClientRect()` 拿触发按钮相对视口的位置，
  算出按钮下边缘到视口底部的距离（`window.innerHeight -
  rect.bottom`）；跟菜单预期高度比较（菜单有 `max-h-[220px]`，可以直接
  用这个数字做比较阈值，不需要精确测量实际渲染高度）——距离小于这个
  阈值时，用另一个 state（比如 `const [openUpward, setOpenUpward] =
  useState(false)`）记下"这次要向上展开"。
- 菜单的 className 根据 `openUpward` 在 `top-full mt-1` 和 `bottom-full
  mb-1` 之间切换（其它 className，如 `absolute left-0 right-0 z-20
  bg-card border ... max-h-[220px] overflow-y-auto`，两种方向共用不变）。
- 这个判断只需要在**打开菜单的那一刻**算一次（点击触发按钮时），不需要
  监听滚动/resize 做持续跟手更新——这是一个简单的下拉框，不是需要
  实时跟随的复杂浮层，做到"打开时判断一次方向"就足够解决用户报的问题
  （固定内容长度的选项列表，一旦决定了展开方向，用户滚动页面时菜单本来
  就应该跟着关掉或保持原状，不需要动态改判断）。
- 具体触发这个判断的位置：现在 `onClick={() => setOpen((v) => !v)}`
  （52 行）是简单的开关切换；改成打开时（从 false 变 true 那一刻）先算
  方向再 `setOpen(true)`，关闭时直接 `setOpen(false)`（不需要重新算
  方向，反正菜单已经收起了）。

**测试**：这是纯浏览器视口相关的展示层逻辑，vitest（jsdom 环境）里
`getBoundingClientRect()`/`window.innerHeight` 的值是可以 mock 的，如果
方便写一个"触发按钮靠近视口底部时 openUpward 应为 true"的单测更好；
写起来有困难或者 jsdom 环境模拟视口这件事比较别扭的话，跑 tsc/eslint/
build 确认无误、并在实现里把判断逻辑写清楚（不要东拼西凑绕过判断）即可，
不强制要求测试覆盖这一条。

---

## 批次 C（前端 only，独立 agent）：#27 技能分类中文翻译 + #28 人物卡准备页弹窗高度统一

这两条改的是完全不同的文件，凑在一个批次只是为了减少并行 agent 数量，
互相之间没有任何关联，可以按任意顺序做。

### C1（#27）技能分类下拉框显示中文

`trpg-frontend/src/routes/games/trpg/character-wizard/components/
SkillFilterBar.tsx`（53-64 行的 `<select>`）：

- `categories: string[]` prop 传进来的是英文枚举值（`combat`/
  `knowledge`/`language`/`perception`/`physical`/`social`/`technical`，
  来自后端 `SkillSpec.category`，这个字段设计上就是英文机器可读标识，
  不要改后端数据）。
- 在 `SkillFilterBar.tsx` 里加一份展示层翻译映射（可以是模块级
  `const SKILL_CATEGORY_LABELS: Record<string, string> = { combat: '战斗',
  knowledge: '知识', language: '语言', perception: '感知', physical:
  '体能', social: '社交', technical: '技术' }`），渲染 `<option>` 的文本
  用 `SKILL_CATEGORY_LABELS[c] ?? c`（映射表里没有的值兜底显示原文，
  防止以后后端新增分类时这里直接显示空白/崩溃）；`value={c}` 属性
  **保持不变**（还是传英文原值，因为 `onCategoryChange`/
  `state.ui.skillCategory` 的比较逻辑用的是这个原始值，不要改这条链路）。

### C2（#28）人物卡准备页弹窗高度统一

`trpg-frontend/src/routes/character-ready/CharacterReadyPage.tsx:28`：

```tsx
<div className="fixed inset-x-0 bottom-0 z-40 bg-card rounded-t-2xl animate-slide-up max-h-[80vh] overflow-y-auto">
```

只设了 `max-h-[80vh]`，没设 `min-h`，导致三个 tab（基本信息/技能/背景
装备）内容长度差异很大时弹窗高度跟着跳动。加一个 `min-h-[60vh]`（跟
`max-h-[80vh]` 搭配，具体数值如果 60vh 看起来不合适可以自己微调，但要
保证：短内容 tab 不会缩得太小、长内容 tab 依然能在 80vh 上限内正常
滚动），改动只涉及这一处 className。

**测试**：C1/C2 都是纯 UI 展示层改动，跑 tsc/eslint/build 确认无误即可，
不强制要求新增测试文件。

---

## 通用要求（三个批次都适用）

- 改完在 `docs/keeper-design/exec/12-真人实测问题清单-追书人.md` 里把
  #23/#24/#25/#27/#28 的状态更新为"已修复（已提交）"，写清楚具体改法
  （跟之前 round1-5 的记录风格一致）。#26 已经是"已核实非 bug"状态，
  不要动它。
- commit message 不要加任何 AI 署名（不要 `Co-Authored-By: Claude`，不要
  `Generated with Claude Code` 这类，任何形式都不要）。
- 完成后把改动了哪些文件、跑了哪些验证命令（pytest / ruff / ty / tsc /
  eslint / build，只有批次 A 涉及后端，批次 B/C 只需要前端命令）如实
  报告，输出要真实可见（不要重定向到 /dev/null，不要串成一条长 `&&`
  链导致某一步失败被掩盖）。
- 不需要做浏览器/e2e 真人测试，真人测试用户会自己做。
