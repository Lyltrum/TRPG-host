# 建卡向导 bug 修复第二轮（exec/12 #11–#17）

> 2026-07-29（同日，round1 三个 commit 之后），用户用重启后的最新代码
> 继续真人实测，报了 7 条新问题。本文档是 Opus 复核代码 + 实测复现
> （直接调用 `compute_preview` 复现，不经浏览器）之后给 Sonnet 的执行
> 规格。跟 round1（`wizard-bugfix-round1.md`）是同一个建卡向导，不重复
> 铺陈已有背景，只讲这一轮新增的诊断与修法。

## 🔴 核心发现：#14/#15/#17 是同一个前端 bug，纯前端，不涉及后端

`useWizardPreview.ts` 把 `state.skillAlloc`（技能"加了多少点"的**增量**）
直接当成 `previewCharacter()` 请求体的 `skills` 字段发给后端。但后端
`compute_preview`/`_compute` 把 `skills[id]` 解释成**这个技能的最终
绝对值**（`current = skills.get(id, base)`），项目里早就有一个专门做
"增量→绝对值"转换的函数 `wizard-network.ts::buildSkillsPayload`
（`useWizardSubmit.ts` 最终提交时用的就是它），但 `useWizardPreview.ts`
从来没调用过它，从建卡向导重制以来这个实时预览请求就一直在发错误数据。

**已用真实后端代码复现验证**（直接调 `compute_preview`，跟 HTTP 端点走
同一段代码，不是猜测）：演员-电影演员（id=4，`EDU*2+APP*2`）职业，八维
属性 STR60/CON60/POW60/DEX60/APP55/SIZ55/INT55/EDU55/LUCK45：

| payload | occupation_skill_points | interest_skill_points | validation |
|---|---|---|---|
| 正确绝对值（格斗:斗殴30/取悦21/恐吓28/汽车驾驶26/信用23） | `budget=220, spent=50, remaining=170` | `budget=110, spent=3` | `[]` |
| 前端实际发送的增量（格斗:斗殴5/取悦6/恐吓13/汽车驾驶6/信用23） | `budget=220, spent=20, remaining=200` | `budget=110, spent=3` | `SKILL_BELOW_BASE ×4` |

第二行跟 exec/12 #14 截图（`20/220，还剩200点`）、#17 截图（"格斗：斗殴
的值 5 不能低于基础值 25"等）分毫不差。`interest_skill_points.spent`
两种 payload 都是 `3`——因为这次复现里唯一进兴趣池的是信用评级溢出
（`credit-rating` 在 `state.skillAlloc` 里存的本来就是绝对值，不受这个
bug影响），不是这个 bug 修好了会变化的部分（#15 是这个 bug 在兴趣步骤的
另一种呈现，不是独立问题）。

**结论**：这不涉及任何后端改动，`compute_preview` 的行为是对的（`skills`
参数本来就该是绝对值，文档/类型注释也是这么写的），错的是前端在
`useWizardPreview.ts` 里没有做转换。

### 修法

`useWizardPreview.ts` 的 `useEffect` 里，构造请求体之前，用 hook 自己
当前持有的 `preview`（上一次成功响应）构造 `skillComputeMap`，再用
`buildSkillsPayload` 转换出绝对值：

```ts
import { buildSkillComputeMap } from './wizard-selectors'
import { buildSkillsPayload } from './wizard-network'

// 在 useEffect 内部，构造请求体之前：
const skillComputeMap = buildSkillComputeMap(preview)  // preview 是本 hook 自己的 state
const skillsPayload = buildSkillsPayload(state.skillAlloc, skillComputeMap)

previewCharacter({
  attributes: state.attr,
  occupationId: state.occupationId,
  skills: skillsPayload,   // 原来这里直接传 state.skillAlloc，改成传转换后的绝对值
  age: state.age,
  generationMethod: state.generationMethod,
  attributePoolTotal: state.attributePoolTotal,
})
```

首次请求（`preview` 还是初始 `null`）时 `skillComputeMap` 是空 map，
`buildSkillsPayload` 会把每项算成 `0 + pts`——但这时 `state.skillAlloc`
通常也是空对象（用户还没分配过任何技能），不构成问题；拿到第一次成功
响应之后，后续每次转换都有正确的 `base` 可用。**这是一个自举关系但不是
死锁**：`base` 只依赖 `attributes`（走公式求值），不依赖 `skills` 传了
什么，所以哪怕第一次请求的 `skills` 是空/不完整的，后端依然会在响应里
把全部技能（含用户还没碰过的）的 `base`/`cap` 算好并放进 `skillView`，
第二次请求就能用上正确的 `skillComputeMap` 了。

**补测试**（这是本轮回归价值最高的一条，之前完全没有测试覆盖这个转换
步骤是否被调用）：在 `trpg-frontend` 侧现有的单测框架里（如果
`useWizardPreview.ts` 目前没有单测文件，新建一个），mock
`previewCharacter`，dispatch 一次 `SET_SKILL_ALLOC` 让某个技能的
`skillAlloc` 从 0 变成 N，断言 `previewCharacter` 实际收到的 `skills`
字段里这个技能的值是 `base + N`（绝对值），而不是 `N`（增量）。如果
`trpg-frontend` 目前没有给 hook 写单测的先例/框架，改成在 e2e 层面加
一条端到端用例也可以（起真实后端、走一遍职业技能加点，断言
`occupation_skill_points.spent` 随加点正确变化）——两种方式选一种，
不要跳过测试。

---

## #11 第 1 步就出现"⚠️ 校验问题"横幅，跟填没填姓名无关

- **根因**（exec/12 #11 已经诊断清楚，这里给出修法）：`ruleset` 到位后
  有个不分步骤都会跑的 `useEffect`（`CharacterWizardPage.tsx`）用
  `FILL_ATTR_DEFAULTS` 把 8 项点数购买属性填上默认值，但不含 `LUCK`
  （幸运只能掷、不参与点数购买）。这个效果在第 1 步就触发，此时
  `state.attr` 有 8 项但缺 `LUCK` 这个键，后端结构性校验"缺键"就会报
  一条 `INVALID_ATTRIBUTES`，在用户走到第 2 步掷幸运之前永远成立。
- **修法**：`useWizardPreview.ts` 的请求 effect 加一个前置条件——只有当
  `ruleset` 存在，且 `state.attr` 已经包含 `ruleset.attributes` 声明的
  **全部**属性键（含 `LUCK`）时才真正发起 preview 请求；否则直接把
  `preview`/`previewError` 清空（或保持不变，不发请求），不产出任何
  校验问题。理由：在属性还没配齐（尤其是幸运没掷）之前，这份草稿本来
  就注定不完整，对它跑规则校验没有意义，不该产出任何"问题"提示给用户看。
  这个判断放在 `useWizardPreview.ts` 内部做，不需要改
  `CharacterWizardPage.tsx` 里那个填默认值的 `useEffect`（那个效果本身
  没错，只是不该被拿去驱动校验横幅）。

## #12 年龄默认 28 岁时应该自动套用，不需要用户主动触发

- **修法**：`AgeStep.tsx` 加一个 `useEffect`，依赖 `attributesReady`/
  `state.ageApplied`/`state.age`：一旦 `attributesReady && !state.
  ageApplied`（且有 `roomId`），自动调用一次 `applyNow(state.age)`，
  不需要等用户碰输入框。用户之后手动改年龄数字，继续走现有的
  `commitAge()`（失焦/回车触发）路径，两条路径不冲突（`applyNow`
  内部本来就有"同一个年龄已经套用过就跳过"的幂等判断，`if (state.
  ageApplied && state.ageAppliedFor === age) return`，重复调用是安全的）。

## #13 去掉"同年龄重掷教育检定"按钮

- **修法**：`AgeStep.tsx` 里 `{state.ageApplied && (<button ...>🎲 同
  年龄重掷教育检定...</button>)}` 这一段整体删除。`applyNow` 函数本身
  不要删（#12 的自动套用还要用它），只删这个手动重掷入口的按钮 JSX。
  删除后检查 `applying` 这个 loading 状态是否还有地方用到（#12 的自动
  套用调用 `applyNow` 时同样会经过 `setApplying(true/false)`，如果
  界面上还需要一个"套用中…"的提示可以保留，具体要不要保留视觉反馈由你
  判断，不强制）。

## #16 校验应该主动引导，不能只是"完成页查看详情"的被动提示

- **背景**：用户否定了 round1 #10① 做的"每步弱提示+完成页详情"这个
  设计方向，但没有指定具体要做成什么样的"强制干预"。这里给出一个明确、
  有限范围的方案（不是推翻重做，是把提示做得更直接）——这个方向已经过
  Opus 思考拍板，照此执行，不需要再跟用户确认设计细节：
  **把 footer 那条横幅从"只报一个数字"升级成"直接列出具体消息"**。
  现状（round1 加的）：
  ```tsx
  {stepMeta.id !== 'finish' && (preview?.validation.length ?? 0) > 0 && (
    <p>⚠️ 当前存在 {preview?.validation.length} 处规则校验问题，可在完成页查看详情</p>
  )}
  ```
  改成直接把 `preview.validation` 的每条 `message` 列出来（不再是
  "个数+跳转完成页"这种间接引用），比如：
  ```tsx
  {stepMeta.id !== 'finish' && (preview?.validation.length ?? 0) > 0 && (
    <div className="mb-2 space-y-1">
      {preview!.validation.map((issue, i) => (
        <p key={i} className="text-[11px] text-[#c04040] text-center">⚠️ {issue.message}</p>
      ))}
    </div>
  )}
  ```
  颜色改成跟 `blockers`/`previewError` 一致的错误红（不再用之前的
  警示黄），因为这些消息现在是直接指出"哪里不对"而不是笼统提醒。
  一旦 #11/#14/#15/#17 的根因修好，这些消息在正常操作路径下应该基本
  不会出现（用户通过 UI 的 +/- 正常加点、后端裁决又是权威且没有 bug 的
  情况下，草稿理应随时合法）——只有当用户真的做出不合法的操作（比如
  #17 那种异常路径）时才会看到具体是哪一条不合法，这就已经是"过程中
  主动告知哪里错了"，不需要更激进的"当场拦住不让加点"这种交互（那样
  风险更高、且不是这批 bug 的必要修复范围）。

## 验证要求

- 后端本轮无改动，不需要跑后端测试套件（除非你在排查过程中发现某个
  改动确实需要动后端——目前诊断认为不需要，如果你执行时发现事实不是
  这样，请如实说明并解释原因，不要为了"看起来符合预期"而回避）。
- `cd trpg-frontend && npx tsc -b` / `npm run lint` / `npm run build`
  全绿，每一项都要真的跑完看到通过。
- 补的测试（单测或 e2e，见"核心发现"章节末尾）要跑通。
- 不需要启动浏览器做点击验证，改完直接汇报，用户会自己测。
