# 建卡向导 bug 修复第三轮（exec/12 #19）

> 2026-07-29，round2（commit `b45dd62`）提交后 Opus 复审发现：round2 对
> #14/#15/#17 的修复**留了一个洞**，在"编辑已有角色卡"这条路径上完全
>失效，而且不会自我纠正。本文档只处理这一个洞（exec/12 #19）。
>
> exec/12 #18（年龄修正重复套用）也是本次复审发现的，但它需要用户先在
> 三个方案里拍板（会改变属性步骤显示的数字），**不在本轮范围**。

## 🔴 问题：`buildSkillsPayload` 的 `?? 0` 静默兜底

`wizard-network.ts::buildSkillsPayload` 目前是：

```ts
for (const [id, pts] of Object.entries(skillAlloc)) {
  if (!pts) continue
  const base = skillComputeMap.get(id)?.base ?? 0   // ← 元凶
  payload[id] = base + pts
}
```

`?? 0` 让"我拿不到这个技能的 base"这件事**无声地退化**成"这个技能的
base 是 0"，于是 `payload[id]` 就等于加点增量本身。这正是 #14/#15/#17
的机制，round2 只修了"调用方忘了调用转换函数"这一层，没有堵住这个
静默兜底本身——所以换一条路径它又出现了。

### 已证明的失效路径（exec/12 #19）

玩家编辑一张已保存的角色卡、且本地没有 localStorage 缓存时：

1. 水合前 `state.attr` 缺 `LUCK`，round2 新加的 `attrsReady` 闸门不发
   请求，`preview` 保持 `null`；
2. `HYDRATE` 一次性把完整 `attr` + 非空 `skillAlloc`（增量）塞进 state；
3. `attrsReady` 翻真 → 发第一次请求，此时 `preview` 仍是 `null` →
   空 map → `?? 0` 兜底 → **又把增量当绝对值发出去**；
4. 响应回来 `setPreview(...)`，但 `preview` **不在 effect 依赖数组里**
   → 不会再发第二次请求 → 错误结果固化。

**实测证明**（临时 vitest 用例，已跑过并删除）：水合后总请求次数 `1`，
`skills = {"fast-talk":5}`，期望 `{"fast-talk":25}`（base 20 + 加点 5）。

### 还会漏进写库路径

`AgeStep` 的 `applyNow()` 调 `syncCurrentStateToBackend(...)`，后者同样
用 `buildSkillsPayload`。`preview` 为空时（比如玩家在水合完成前就点到了
年龄步骤），它会把增量当绝对值 **PATCH 进数据库**。这比显示错误更严重。

---

## 修法

### 1. `wizard-network.ts::buildSkillsPayload` 改成失败要响

不再 `?? 0` 静默兜底。缺 base 就抛一个具名错误，让每个调用方显式决定
怎么办：

```ts
/** `buildSkillsPayload` 拿不到某个技能的权威 base 时抛出——绝不用 0
 * 兜底（那会把"加了多少点"直接当成"最终值"发出去，见
 * wizard-bugfix-round3.md）。 */
export class MissingSkillBaseError extends Error {
  constructor(public readonly skillId: string) {
    super(`技能 ${skillId} 还没有后端权威的基础值，无法换算成最终值`)
    this.name = 'MissingSkillBaseError'
  }
}

export function buildSkillsPayload(
  skillAlloc: Record<string, number>,
  skillComputeMap: Map<string, SkillComputeView>
): Record<string, number> {
  const payload: Record<string, number> = {}
  for (const [id, pts] of Object.entries(skillAlloc)) {
    if (!pts) continue
    const base = skillComputeMap.get(id)?.base
    if (base == null) throw new MissingSkillBaseError(id)
    payload[id] = base + pts
  }
  return payload
}
```

注意 `if (!pts) continue` 要保留（加点为 0 的技能本来就不该出现在
payload 里），所以"map 为空 + skillAlloc 也为空"不会抛错，新建角色的
首次请求不受影响。

### 2. `useWizardPreview.ts`：首次请求降级 + 拿到 base 后补发一次

两处改动：

**(a) 转换失败时降级成"引导请求"**——不带 skills 发出去，它的唯一目的
就是把全部技能的 `base`/`cap` 取回来：

```ts
let skillsPayload: Record<string, number>
try {
  skillsPayload = buildSkillsPayload(state.skillAlloc, buildSkillComputeMap(preview))
} catch {
  // 还没有任何权威 base（首次请求 / 水合刚落地）——这次只用来把 base 取
  // 回来，不带技能分配，避免把增量当绝对值发出去。下面 baseMapReady 翻真
  // 后会自动补发一次带正确绝对值的请求。
  skillsPayload = {}
}
```

**(b) 把"有没有 base 数据"加进依赖数组**，让引导请求之后自动补发一次：

```ts
const baseMapReady = preview !== null
// ...
}, [
  ruleset,
  attrsReady,
  baseMapReady,          // ← 新增
  state.attr,
  ...
])
```

`preview` 只会从 `null` 翻成非 `null` 一次（除非 `attrsReady` 掉下去把
它清空），所以最多多一次 400ms 防抖请求，不会循环。**保留**原有的
`// eslint-disable-next-line react-hooks/exhaustive-deps`（effect 内部
仍然读 `preview` 本身，不能直接进依赖，否则每次响应都会重新触发）。

### 3. `useWizardSubmit.ts`：转换失败就明确报错，不提交

绝不能静默丢掉玩家的加点、也不能发garbage：

```ts
let skillsPayload: Record<string, number>
try {
  skillsPayload = buildSkillsPayload(state.skillAlloc, skillComputeMap)
} catch {
  setSubmitError('规则数据还没加载完，请稍候重试')
  setSubmitting(false)
  return
}
```
（具体写法按现有 `handleSubmit` 的 try/finally 结构调整，保证
`setSubmitting(false)` 一定被执行，不要重复调用。）

### 4. `AgeStep.tsx`：让异常走到已有的 catch，不要写库

`syncCurrentStateToBackend` 现在可能抛 `MissingSkillBaseError`，
`applyNow` 已经有 `try/catch` 包着并 `setError(friendlyErrorMessage(...))`，
所以**默认行为已经是对的**（不会写库、会显示错误）。你只需要确认这一点，
必要时把错误文案改得更明确一点（比如识别 `MissingSkillBaseError` 时显示
"规则数据还没加载完，请稍候重试"）。不要为此重构 `applyNow`。

### 5. 补一条**永久**回归测试（本轮最重要的产出）

在已有的 `useWizardPreview.test.tsx` 里新增一个 case，覆盖 #19 的水合
路径（不是 round2 那条已有的"连续两次渲染"用例，那条覆盖不到这个洞）：

- 第一次渲染：`attr` 只有部分属性（缺 LUCK）→ 断言 `previewCharacter`
  被调用 **0** 次；
- 第二次渲染（模拟 HYDRATE 一次性到位）：`attr` 完整 + `skillAlloc`
  非空 → 等待足够长时间（≥900ms，要覆盖两次 400ms 防抖 + 响应）→
  断言**最后一次**请求的 `skills` 是绝对值 `{ 'fast-talk': 25 }`。

同时**必须做变异检验**：把 `buildSkillsPayload` 的 `if (base == null)
throw` 临时改回 `?? 0`，确认这条新测试真的报红；改回来后确认重新全绿。
把变异检验的结果写进你的总结里。

---

## 验证要求

每一项都要真的跑完看到输出，不要重定向到 `/dev/null`、不要串成一条
`&&` 长链让失败被掩盖：

- `cd /Users/apple/Developer/work/AIDM_ALL/TRPG-master/trpg-frontend && npx tsc -b`
- `cd /Users/apple/Developer/work/AIDM_ALL/TRPG-master/trpg-frontend && npm run lint`
- `cd /Users/apple/Developer/work/AIDM_ALL/TRPG-master/trpg-frontend && npm run build`
- `cd /Users/apple/Developer/work/AIDM_ALL/TRPG-master/trpg-frontend && npm run test`（round2 已有的用例 + 本轮新增的，都要绿）
- 变异检验（见上）

本轮**不涉及后端**，不需要跑后端测试。不需要浏览器点击验证。

⚠️ Bash 工具不要用裸 `cd xxx` 残留切换工作目录（会污染这个持久 shell
会话），每条命令用 `cd 绝对路径 && command` 的形式。
