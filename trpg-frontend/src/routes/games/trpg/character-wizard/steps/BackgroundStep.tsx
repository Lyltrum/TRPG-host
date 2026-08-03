import { BACKGROUND_DETAIL_FIELDS } from '@/data/character-model'
import { StepShell, StepSection } from '../components/StepShell'
import type { WizardAction, WizardState } from '../wizard-state'

/** 步 6 · 角色故事（重制设计 v2 §6-步骤6）：装备/自由背景/8 个引导字段/备注，
 * 全部可空。 */
export function BackgroundStep({ state, dispatch }: { state: WizardState; dispatch: (action: WizardAction) => void }) {
  const textareaClass =
    'wz-field notepaper w-full px-2.5 py-1.5 text-[13px] resize-none'

  return (
    <StepShell lead="不会写可以先空着，随时可以回来补。">
      <StepSection title="装备与物品">
        <textarea
          value={state.equipment}
          onChange={(e) => dispatch({ type: 'SET_EQUIPMENT', value: e.target.value })}
          placeholder="手电筒、笔记本、相机、急救包…"
          rows={3}
          className={textareaClass}
        />
      </StepSection>

      <StepSection title="背景故事">
        <textarea
          value={state.background}
          onChange={(e) => dispatch({ type: 'SET_BACKGROUND', value: e.target.value })}
          placeholder="简单描述你的角色背景…"
          rows={4}
          className={textareaClass}
        />
      </StepSection>

      <StepSection title="背景故事细节（可选）">
        <div className="space-y-3">
          {BACKGROUND_DETAIL_FIELDS.map(({ key, label, placeholder }) => (
            <div key={key}>
              <label className="text-[11px] font-medium text-text-muted mb-1 block">{label}</label>
              <textarea
                value={state.backgroundDetail[key]}
                onChange={(e) => dispatch({ type: 'SET_BACKGROUND_DETAIL', key, value: e.target.value })}
                placeholder={placeholder}
                rows={2}
                className={`${textareaClass} text-[13px]`}
              />
            </div>
          ))}
        </div>
      </StepSection>

      <StepSection title="其他备注">
        <textarea
          value={state.notes}
          onChange={(e) => dispatch({ type: 'SET_NOTES', value: e.target.value })}
          placeholder="角色特质、秘密、人际关系…"
          rows={3}
          className={textareaClass}
        />
      </StepSection>
    </StepShell>
  )
}
