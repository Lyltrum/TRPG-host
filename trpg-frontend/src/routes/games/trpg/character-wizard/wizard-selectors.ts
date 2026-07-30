import type { CharacterComputeResult, Ruleset, SkillComputeView } from 'trpg-sdk'
import type { OccupationSpec } from '@/data/types'
import type { GenerationMethod, WizardState } from './wizard-state'
import type { WizardStepId } from './wizard-steps'

/**
 * 建卡向导的纯派生计算（重制设计 v2 §7 职责边界）：从 `(state, ruleset,
 * preview)` 算出显示用的量和门禁清单。**任何 COC7 规则数值（预算/base/cap/
 * 分账）一律来自 preview，这里绝不本地重算**——点数购买法的预算/上下限这类
 * "静态配置"数据（来自 ruleset.attributePointBuy，不是规则引擎的裁决结果）
 * 例外，§4 已经论证这部分前端算术是安全的。
 */

export function sumValues(record: Record<string, number>): number {
  return Object.values(record).reduce((a, b) => a + b, 0)
}

/** 角色的**有效属性**：年龄修正之后的最终值；还没套用过年龄修正时就是
 * 分配值本身。衍生值/技能基础值/职业技能点公式/角色卡展示一律用它；
 * 属性分配 UI 和三条生成方法约束（预算/池值/步进）用 `state.attr`。 */
export function effectiveAttr(state: WizardState): Record<string, number> {
  return state.attrAfterAge ?? state.attr
}

export function normalizeDerivedStats(d: CharacterComputeResult['derivedStats'] | undefined) {
  const num = (v: unknown) => (typeof v === 'number' ? v : 0)
  const str = (v: unknown) => (v == null ? '0' : String(v))
  return {
    hp: num(d?.HP),
    san: num(d?.SAN),
    mp: num(d?.MP),
    db: str(d?.DB),
    move: num(d?.MOV),
  }
}

export function buildSkillComputeMap(preview: CharacterComputeResult | null): Map<string, SkillComputeView> {
  const map = new Map<string, SkillComputeView>()
  preview?.skillView.forEach((v) => map.set(v.id, v))
  return map
}

/** 每步的硬门禁上下文需要的类型别名，供 CharacterWizardPage 组合调用。 */
export function pointBuyAttributes(ruleset: Ruleset | null) {
  return (ruleset?.attributes ?? []).filter((a) => a.pointBuy)
}

export function nonPointBuyAttributes(ruleset: Ruleset | null) {
  return (ruleset?.attributes ?? []).filter((a) => !a.pointBuy)
}

// 掷点池模式下单项属性的合法范围——后端硬编码常量（coc7_rules.py 的
// ROLL_POOL_ATTRIBUTE_MIN/MAX），是骰子公式本身的产出范围，不是 ruleset 里
// 可调的约束，跟点数购买法的 [10,90] 不共用。
export const ROLL_POOL_ATTRIBUTE_MIN = 15
export const ROLL_POOL_ATTRIBUTE_MAX = 90

export function attrRange(ruleset: Ruleset | null, generationMethod: GenerationMethod) {
  const rules = ruleset?.attributePointBuy ?? null
  const min = generationMethod === 'roll_pool' ? ROLL_POOL_ATTRIBUTE_MIN : (rules?.minValue ?? ROLL_POOL_ATTRIBUTE_MIN)
  const max = rules?.maxValue ?? ROLL_POOL_ATTRIBUTE_MAX
  return { min, max }
}

export function attrBudget(
  ruleset: Ruleset | null,
  generationMethod: GenerationMethod,
  attributePoolTotal: number | null
): number {
  if (generationMethod === 'roll_pool') return attributePoolTotal ?? 0
  return ruleset?.attributePointBuy?.budget ?? 0
}

export function attrDefaultValue(ruleset: Ruleset | null): number {
  return ruleset?.attributePointBuy?.defaultValue ?? 0
}

export function attrPointsTotal(attr: Record<string, number>, attrs: ReturnType<typeof pointBuyAttributes>): number {
  return attrs.reduce((sum, a) => sum + (attr[a.key] ?? 0), 0)
}

/**
 * 平均分配算法（照搬 coc-char-gen `evenDistributePool`，§6-步1-②）：
 * 1. base = clamp(floor(total / 8 / 5) * 5, min, max)，全部属性先设成 base；
 * 2. 剩余按固定顺序轮转 +5（跳过已到 max 的项），直到分完。
 * 本项目的预算/单项范围组合下总能精确分完（480/8=60 整除；掷点池总值是
 * 5 的倍数且落在 [195,720]，八项 [15,90] 一定能装下）。
 */
export function computeEvenDistribution(total: number, min: number, max: number, keys: string[]): Record<string, number> {
  const step = 5
  const result: Record<string, number> = {}
  if (keys.length === 0) return result
  let base = Math.floor(total / keys.length / step) * step
  base = Math.max(min, Math.min(max, base))
  keys.forEach((k) => {
    result[k] = base
  })
  let remain = total - base * keys.length
  let i = 0
  let guard = 0
  while (remain >= step && guard < keys.length * 200) {
    const k = keys[i % keys.length]
    if (result[k] + step <= max) {
      result[k] += step
      remain -= step
    }
    i++
    guard++
  }
  return result
}

/**
 * ★ 职业技能集合 = `occupation.skillIds` ∪ flatten(`slotPicks`) ∪
 * `{credit-rating}`。编辑已有卡（`slotPicks` 全空）时用
 * `preview.slotOccupiedSkillIds` 补上（§4-B），否则 ★ 列表会退化成只有
 * 固定技能。
 */
export function occupationStarSkillIds(
  occupation: OccupationSpec | null,
  slotPicks: string[][],
  preview: CharacterComputeResult | null
): Set<string> {
  const ids = new Set<string>(occupation?.skillIds ?? [])
  ids.add('credit-rating')
  const flatPicks = slotPicks.flat().filter(Boolean)
  if (flatPicks.length > 0) {
    flatPicks.forEach((id) => ids.add(id))
  } else if (preview?.slotOccupiedSkillIds) {
    preview.slotOccupiedSkillIds.forEach((id) => ids.add(id))
  }
  return ids
}

// 建卡阶段不可分配的技能（后端 coc7_rules.py::NON_ALLOCATABLE_SKILL_IDS，
// 规则明文禁止——克苏鲁神话只能通过游戏内事件习得，不能在建卡时手动加点）。
// 跟 ROLL_POOL_ATTRIBUTE_MIN/MAX 一样是后端硬编码常量，不是 ruleset 里可调
// 的数据，前端镜像一份纯粹是为了禁用 +/- 按钮，后端才是真正的裁决方。
export const NON_ALLOCATABLE_SKILL_IDS = new Set<string>(['cthulhu-mythos'])

export function totalPointsRemaining(preview: CharacterComputeResult | null, pendingDelta: number): number {
  const occBudget = preview?.occupationSkillPoints.budget ?? 0
  const occSpent = preview?.occupationSkillPoints.spent ?? 0
  const intBudget = preview?.interestSkillPoints.budget ?? 0
  const intSpent = preview?.interestSkillPoints.spent ?? 0
  return Math.max(0, occBudget + intBudget - occSpent - intSpent - pendingDelta)
}

/**
 * 兴趣点数自己的剩余——**不夹到 0**，可以是负数（区别于 `totalPointsRemaining`）。
 *
 * 后端 `coc7_rules.py::_compute` 的瀑布记账只单向成立：职业技能超出职业
 * 预算的部分会溢出记进 `interest_spent`（`occupation_spent` 因此恒不超过
 * `occupation_budget`），但**反向不成立**——非职业技能只能由兴趣点买，
 * 职业池的剩余额度不能倒过来补贴兴趣池超支。后端对此有独立的硬校验
 * （`INTEREST_POINTS_EXCEEDED`），不看两池合计够不够。
 *
 * 所以合计剩余（`totalPointsRemaining`）为正，不代表兴趣点没有超支——两个
 * 数字口径不同，不能共用同一个"还剩 X 点"文案，否则会出现"兴趣点数
 * 114/110 已经超了，但旁边写着还剩 61 点"这种自相矛盾的显示（且据此拦
 * "下一步"会漏掉这类超支）。 */
export function interestPointsRemaining(preview: CharacterComputeResult | null, pendingDelta: number): number {
  const intBudget = preview?.interestSkillPoints.budget ?? 0
  const intSpent = preview?.interestSkillPoints.spent ?? 0
  return intBudget - intSpent - pendingDelta
}

export interface WizardContext {
  ruleset: Ruleset | null
  preview: CharacterComputeResult | null
  pendingDelta: number
}

/** 每步的硬门禁原因（非空即拦住"下一步"）。职业/兴趣技能两步是软门禁，
 * 交给 CharacterWizardPage 的确认条处理，这里不返回拦截项。 */
export function stepBlockers(stepId: WizardStepId, state: WizardState, ctx: WizardContext): string[] {
  switch (stepId) {
    case 'concept': {
      const reasons: string[] = []
      if (!state.info.name.trim()) reasons.push('请填写角色姓名')
      if (!state.info.gender) reasons.push('请选择性别')
      return reasons
    }

    case 'attrs': {
      const reasons: string[] = []
      if (state.generationMethod !== 'roll') {
        const attrs = pointBuyAttributes(ctx.ruleset)
        const budget = attrBudget(ctx.ruleset, state.generationMethod, state.attributePoolTotal)
        const total = attrPointsTotal(state.attr, attrs)
        if (state.generationMethod === 'roll_pool') {
          if (state.attributePoolTotal == null) {
            reasons.push('请先掷点池')
          } else if (total !== budget) {
            reasons.push(total > budget ? `多花了 ${total - budget} 点` : `还剩 ${budget - total} 点没分完`)
          }
        } else if (total > budget) {
          reasons.push(`多花了 ${total - budget} 点`)
        }
      }
      if (state.attr.LUCK == null) reasons.push('请先掷幸运')
      return reasons
    }

    case 'age':
      return state.ageApplied ? [] : ['请先套用年龄调整']

    case 'occupation':
      return state.occupationId == null ? ['请先选择职业'] : []

    case 'occPoints':
    case 'intPoints':
      return []

    case 'background':
      return []

    case 'finish':
      return (ctx.preview?.validation ?? []).map((v) => v.message)

    default:
      return []
  }
}
