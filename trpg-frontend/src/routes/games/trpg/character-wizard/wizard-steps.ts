// 建卡向导 8 步的单一事实来源（重制设计 v2 §5）。步数/顺序只在这里改，
// CharacterWizardPage 的进度条和 steps/*.tsx 的路由分发都读这份常量。
export type WizardStepId =
  | 'concept'
  | 'attrs'
  | 'age'
  | 'occupation'
  | 'occPoints'
  | 'intPoints'
  | 'background'
  | 'finish'

export interface WizardStepMeta {
  id: WizardStepId
  title: string
  short: string
}

export const WIZARD_STEPS: WizardStepMeta[] = [
  { id: 'concept', title: '基本信息', short: '信息' },
  { id: 'attrs', title: '属性与幸运', short: '属性' },
  { id: 'age', title: '年龄', short: '年龄' },
  { id: 'occupation', title: '选职业', short: '职业' },
  { id: 'occPoints', title: '职业技能', short: '职业技能' },
  { id: 'intPoints', title: '兴趣技能', short: '兴趣技能' },
  { id: 'background', title: '角色故事', short: '故事' },
  { id: 'finish', title: '完成', short: '完成' },
]
