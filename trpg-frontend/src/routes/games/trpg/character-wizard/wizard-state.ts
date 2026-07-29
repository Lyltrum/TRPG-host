import type { AgeAdjustmentResult, AttributePoolRollView } from 'trpg-sdk'
import type { BackgroundDetail } from '@/data/character-model'
import { emptyBackgroundDetail } from '@/data/character-model'

/**
 * 建卡向导的状态与 reducer（重制设计 v2 §8）。
 *
 * 单个 `useReducer`，不引入新 store：向导状态是一次性、随页面卸载即弃的，
 * 真正的持久化在后端 + character-store 缓存。这里的价值是把散落在各个
 * setter 里的不变量（改属性要作废年龄、换职业要重置槽位与信用……）收进
 * 一处 reducer 分支，而不是让每个组件各自记得要同步维护哪些状态。
 *
 * 边界（§7 职责边界表）：这个文件只改 state，不发请求、不读 ruleset、
 * 不算任何 COC7 规则数值——凡是需要 ruleset/preview 才能算出来的值
 * （预算/上限/剩余点数……）由调用方（steps/*.tsx）算好后再 dispatch。
 */

export type GenerationMethod = 'pointbuy' | 'roll' | 'roll_pool'

export interface WizardInfo {
  name: string
  playerName: string
  gender: string
  residence: string
  birthplace: string
}

export interface LuckRoll {
  kind: string
  dice: number[]
  value: number
}

export type SkillFilter = 'all' | 'occupation' | 'allocated'

export interface WizardUiState {
  occSearch: string
  skillSearch: string
  skillFilter: SkillFilter
  skillCategory: string | null
}

export interface WizardState {
  step: number
  info: WizardInfo
  age: number
  // 分配值：玩家在属性步骤分配出来的原始点数（wizard-bugfix-round4.md
  // 方案 A）。点数预算 / 掷点池总和 / 步进为 5 这三条生成方法约束，以及
  // 年龄修正的计算基准，都只对这份数据成立——`APPLY_AGE_SUCCESS` 不再
  // 覆盖它，年龄修正的结果单独存进 `attrAfterAge`。
  attr: Record<string, number>
  attrInputs: Record<string, string>
  generationMethod: GenerationMethod
  attributePoolTotal: number | null
  poolRolls: AttributePoolRollView[]
  luckRoll: LuckRoll | null
  ageApplied: boolean
  ageAppliedFor: number | null
  ageResult: AgeAdjustmentResult | null
  // 有效值：年龄修正之后的最终属性（wizard-bugfix-round4.md 方案 A）。
  // 衍生值（HP/SAN/MP/DB/MOV）、技能基础值、职业技能点公式、角色卡展示
  // 一律该用它，不用 `attr`——两者不能合并成一份，否则要么年龄修正的
  // "分配值→有效值"变换被反复套用（#18：属性被越扣越多），要么有效值被
  // 拿去跑只对分配值成立的预算/池值/步进校验（#20：掷点池角色年龄修正后
  // 永远无法完成建卡）。还没套用过年龄修正、或年龄状态被作废时为 `null`，
  // 消费方应回落到 `attr`（见 `effectiveAttr` 选择器）。
  attrAfterAge: Record<string, number> | null
  occupationId: number | null
  // 纯 UI 状态，不提交给后端（重制设计 v2 §3）——玩家在自选槽里"选的"技能，
  // 只用于决定职业技能步骤 ★ 列表显示哪些技能，提交时依旧只发 skillAlloc。
  slotPicks: string[][]
  // 技能加点的唯一真相：skillId → 加了多少点。不再有 interestAlloc 影子状态
  // （§6-步骤5），职业/兴趣分账完全读后端 preview.*.spent。
  skillAlloc: Record<string, number>
  equipment: string
  background: string
  notes: string
  backgroundDetail: BackgroundDetail
  ui: WizardUiState
}

/**
 * HYDRATE 补丁的类型——嵌套对象（`info`/`backgroundDetail`/`ui`）允许部分
 * 字段，reducer 会对它们做浅合并而不是整体替换（比如 `playerName` 从不
 * 落后端，水合补丁不带这个键时不能把它清空，见下面 HYDRATE 分支）。
 */
export interface WizardHydratePatch extends Partial<Omit<WizardState, 'info' | 'backgroundDetail' | 'ui'>> {
  info?: Partial<WizardInfo>
  backgroundDetail?: Partial<BackgroundDetail>
  ui?: Partial<WizardUiState>
}

export const DEFAULT_AGE = 28

export function createInitialWizardState(): WizardState {
  return {
    step: 0,
    info: { name: '', playerName: '', gender: '', residence: '', birthplace: '' },
    age: DEFAULT_AGE,
    attr: {},
    attrInputs: {},
    generationMethod: 'roll_pool',
    attributePoolTotal: null,
    poolRolls: [],
    luckRoll: null,
    ageApplied: false,
    ageAppliedFor: null,
    ageResult: null,
    attrAfterAge: null,
    occupationId: null,
    slotPicks: [],
    skillAlloc: {},
    equipment: '',
    background: '',
    notes: '',
    backgroundDetail: emptyBackgroundDetail(),
    ui: { occSearch: '', skillSearch: '', skillFilter: 'all', skillCategory: null },
  }
}

export type WizardAction =
  | { type: 'SET_STEP'; step: number }
  | { type: 'SET_INFO'; patch: Partial<WizardInfo> }
  | { type: 'SET_AGE'; age: number }
  | { type: 'FILL_ATTR_DEFAULTS'; defaults: Record<string, number> }
  | { type: 'SET_ATTR_VALUE'; key: string; value: number }
  | { type: 'SET_ATTR_BULK'; patch: Record<string, number> }
  | { type: 'SET_ATTR_INPUT'; key: string; value: string }
  | { type: 'SET_ATTR_INPUTS'; patch: Record<string, string> }
  | { type: 'SET_GENERATION_METHOD'; method: GenerationMethod }
  | { type: 'ROLL_ATTRIBUTES_SUCCESS'; attributes: Record<string, number>; attrInputs: Record<string, string> }
  | {
      type: 'ROLL_POOL_SUCCESS'
      rolls: AttributePoolRollView[]
      total: number
      resetAttr: Record<string, number>
      resetAttrInputs: Record<string, string>
    }
  | { type: 'SET_LUCK_ROLL'; luckRoll: LuckRoll }
  | { type: 'APPLY_AGE_SUCCESS'; result: AgeAdjustmentResult }
  | { type: 'SET_OCCUPATION'; occupationId: number; slotCounts: number[]; creditMin: number; creditMax: number }
  | { type: 'SET_SLOT_PICK'; slotIndex: number; pickIndex: number; skillId: string }
  | { type: 'SET_SKILL_ALLOC'; skillId: string; value: number }
  | { type: 'SET_EQUIPMENT'; value: string }
  | { type: 'SET_BACKGROUND'; value: string }
  | { type: 'SET_NOTES'; value: string }
  | { type: 'SET_BACKGROUND_DETAIL'; key: keyof BackgroundDetail; value: string }
  | { type: 'SET_UI'; patch: Partial<WizardUiState> }
  | { type: 'HYDRATE'; patch: WizardHydratePatch }

// 改任何一项影响属性的动作都要作废年龄调整（§11 风险 2：改单项/平均分配/
// 清空/重掷/切生成方式全部要归到这里，漏一条就会让玩家带着套在旧属性上的
// 年龄修正提交）。`attrAfterAge` 是上一次年龄修正的结果，分配值一变它就
// 不再对应当前的 `attr`，一并清空（方案 A）。
function invalidateAge(): Pick<WizardState, 'ageApplied' | 'ageAppliedFor' | 'attrAfterAge'> {
  return { ageApplied: false, ageAppliedFor: null, attrAfterAge: null }
}

export function wizardReducer(state: WizardState, action: WizardAction): WizardState {
  switch (action.type) {
    case 'SET_STEP':
      return { ...state, step: action.step }

    case 'SET_INFO':
      return { ...state, info: { ...state.info, ...action.patch } }

    case 'SET_AGE':
      if (action.age === state.age) return state
      return { ...state, age: action.age, ...invalidateAge() }

    case 'FILL_ATTR_DEFAULTS': {
      const filled = { ...state.attr }
      let changed = false
      for (const [key, value] of Object.entries(action.defaults)) {
        if (typeof filled[key] !== 'number') {
          filled[key] = value
          changed = true
        }
      }
      return changed ? { ...state, attr: filled } : state
    }

    case 'SET_ATTR_VALUE':
      return {
        ...state,
        attr: { ...state.attr, [action.key]: action.value },
        attrInputs: { ...state.attrInputs, [action.key]: String(action.value) },
        ...invalidateAge(),
      }

    case 'SET_ATTR_BULK':
      return {
        ...state,
        attr: { ...state.attr, ...action.patch },
        attrInputs: {
          ...state.attrInputs,
          ...Object.fromEntries(Object.entries(action.patch).map(([k, v]) => [k, String(v)])),
        },
        ...invalidateAge(),
      }

    case 'SET_ATTR_INPUT':
      return { ...state, attrInputs: { ...state.attrInputs, [action.key]: action.value } }

    case 'SET_ATTR_INPUTS':
      return { ...state, attrInputs: { ...state.attrInputs, ...action.patch } }

    case 'SET_GENERATION_METHOD':
      return {
        ...state,
        generationMethod: action.method,
        ...(action.method === 'pointbuy' ? { attributePoolTotal: null, poolRolls: [] } : null),
      }

    case 'ROLL_ATTRIBUTES_SUCCESS':
      return {
        ...state,
        attr: action.attributes,
        attrInputs: action.attrInputs,
        generationMethod: 'roll',
        attributePoolTotal: null,
        poolRolls: [],
        // 服务端权威掷骰会把幸运也一并掷出（作为 attributes 的一部分），
        // 旧的单独掷骰明细（dice 数组）不再对应这次的值，清空避免显示错配。
        luckRoll: null,
        ...invalidateAge(),
      }

    case 'ROLL_POOL_SUCCESS':
      return {
        ...state,
        attr: { ...state.attr, ...action.resetAttr },
        attrInputs: { ...state.attrInputs, ...action.resetAttrInputs },
        attributePoolTotal: action.total,
        poolRolls: action.rolls,
        generationMethod: 'roll_pool',
        ...invalidateAge(),
      }

    case 'SET_LUCK_ROLL':
      // 幸运独立掷，但同时写回 attr.LUCK——它仍然是"属性"的一部分（提交/
      // 导出/衍生值计算都从 attr 读），只是不占八维分配的预算。
      return {
        ...state,
        luckRoll: action.luckRoll,
        attr: { ...state.attr, LUCK: action.luckRoll.value },
      }

    case 'APPLY_AGE_SUCCESS':
      // 分配值（attr/attrInputs）不变——年龄修正的结果只写进 attrAfterAge
      // （方案 A）。此前这里直接把 attributesAfter 覆盖回 attr，导致下一次
      // 套用会在已经修正过的值上再修一次（#18：属性被越扣越多）。
      return {
        ...state,
        attrAfterAge: action.result.attributesAfter,
        ageApplied: true,
        ageAppliedFor: action.result.age,
        ageResult: action.result,
      }

    case 'SET_OCCUPATION': {
      // 信用评级夹到新职业区间，但不覆盖玩家已经手动调过、且在新区间内仍然
      // 合法的值（照搬现状逻辑）。
      const current = state.skillAlloc['credit-rating']
      const clampedCredit =
        current == null ? action.creditMin : Math.max(action.creditMin, Math.min(action.creditMax, current))
      return {
        ...state,
        occupationId: action.occupationId,
        slotPicks: action.slotCounts.map((count) => Array(count).fill('')),
        skillAlloc: { ...state.skillAlloc, 'credit-rating': clampedCredit },
      }
    }

    case 'SET_SLOT_PICK': {
      const slots = state.slotPicks.map((picks, i) =>
        i === action.slotIndex ? picks.map((p, j) => (j === action.pickIndex ? action.skillId : p)) : picks
      )
      return { ...state, slotPicks: slots }
    }

    case 'SET_SKILL_ALLOC':
      return { ...state, skillAlloc: { ...state.skillAlloc, [action.skillId]: Math.max(0, action.value) } }

    case 'SET_EQUIPMENT':
      return { ...state, equipment: action.value }

    case 'SET_BACKGROUND':
      return { ...state, background: action.value }

    case 'SET_NOTES':
      return { ...state, notes: action.value }

    case 'SET_BACKGROUND_DETAIL':
      return { ...state, backgroundDetail: { ...state.backgroundDetail, [action.key]: action.value } }

    case 'SET_UI':
      return { ...state, ui: { ...state.ui, ...action.patch } }

    case 'HYDRATE': {
      const { patch } = action
      return {
        ...state,
        ...patch,
        // 嵌套对象要做浅合并而不是整体替换——比如 playerName 从来不落后端
        // （CharacterRead 没有这个字段），水合补丁不带这个键时不能把它清空。
        info: patch.info ? { ...state.info, ...patch.info } : state.info,
        backgroundDetail: patch.backgroundDetail
          ? { ...state.backgroundDetail, ...patch.backgroundDetail }
          : state.backgroundDetail,
        ui: patch.ui ? { ...state.ui, ...patch.ui } : state.ui,
      }
    }

    default:
      return state
  }
}
