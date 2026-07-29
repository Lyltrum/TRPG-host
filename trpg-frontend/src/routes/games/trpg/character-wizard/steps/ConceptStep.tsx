import type { Ruleset } from 'trpg-sdk'
import { StepShell, StepSection } from '../components/StepShell'
import type { WizardAction, WizardState } from '../wizard-state'

/** 步 0 · 基本信息（重制设计 v2 §6-步骤0）。 */
export function ConceptStep({
  state,
  dispatch,
}: {
  state: WizardState
  dispatch: (action: WizardAction) => void
  ruleset: Ruleset
}) {
  const { info } = state
  return (
    <StepShell title="基本信息" lead="先给你的调查员起个名字，其余信息随时可以回来改。">
      <StepSection title="调查员信息">
        <div className="space-y-3">
          <input
            value={info.name}
            onChange={(e) => dispatch({ type: 'SET_INFO', patch: { name: e.target.value } })}
            placeholder="角色姓名"
            className="w-full px-3.5 py-2.5 rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] outline-none focus:border-brass"
          />
          <input
            value={info.playerName}
            onChange={(e) => dispatch({ type: 'SET_INFO', patch: { playerName: e.target.value } })}
            placeholder="玩家名（可选，默认同角色姓名）"
            className="w-full px-3.5 py-2.5 rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] outline-none focus:border-brass"
          />
          <div>
            <label className="text-[11px] font-medium text-text-muted mb-1 block">性别</label>
            <select
              value={info.gender}
              onChange={(e) => dispatch({ type: 'SET_INFO', patch: { gender: e.target.value } })}
              className="w-full px-3.5 py-2.5 rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] outline-none focus:border-brass"
            >
              <option value="" disabled>
                请选择性别
              </option>
              <option>男</option>
              <option>女</option>
              <option>其他</option>
            </select>
          </div>
          <div className="grid grid-cols-2 gap-2.5">
            <div>
              <label className="text-[11px] font-medium text-text-muted mb-1 block">居住地</label>
              <input
                value={info.residence}
                onChange={(e) => dispatch({ type: 'SET_INFO', patch: { residence: e.target.value } })}
                className="w-full px-3.5 py-2.5 rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] outline-none focus:border-brass"
              />
            </div>
            <div>
              <label className="text-[11px] font-medium text-text-muted mb-1 block">出生地</label>
              <input
                value={info.birthplace}
                onChange={(e) => dispatch({ type: 'SET_INFO', patch: { birthplace: e.target.value } })}
                className="w-full px-3.5 py-2.5 rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] outline-none focus:border-brass"
              />
            </div>
          </div>
        </div>
      </StepSection>
    </StepShell>
  )
}
