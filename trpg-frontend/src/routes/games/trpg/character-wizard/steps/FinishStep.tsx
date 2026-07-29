import { useMemo } from 'react'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { StepShell, StepSection } from '../components/StepShell'
import { normalizeDerivedStats } from '../wizard-selectors'
import type { WizardState } from '../wizard-state'

/** 步 7 · 完成（重制设计 v2 §6-步骤7）：摘要 + 校验结果 + 主要技能。
 * "完成创建"按钮在 CharacterWizardPage 的 footer 里（复用现状提交流程）——
 * 点击后会自动把角色数据存进后端数据库，不依赖用户是否额外导出过。 */
export function FinishStep({
  state,
  ruleset,
  preview,
}: {
  state: WizardState
  ruleset: Ruleset
  preview: CharacterComputeResult | null
}) {
  const selectedOcc = useMemo(() => ruleset.occupations.find((o) => o.id === state.occupationId) ?? null, [ruleset, state.occupationId])
  const derived = normalizeDerivedStats(preview?.derivedStats)

  const mainSkills = useMemo(() => {
    if (!preview) return []
    const nameById = new Map(ruleset.skills.map((s) => [s.id, s.name]))
    return [...preview.skillView]
      .filter((v) => v.current > v.base || v.id === 'credit-rating')
      .sort((a, b) => b.current - a.current)
      .slice(0, 16)
      .map((v) => ({ id: v.id, name: nameById.get(v.id) ?? v.id, value: v.current }))
  }, [preview, ruleset])

  return (
    <StepShell title="完成" lead="确认一下角色摘要，看看有没有校验提示，点击下方「完成创建」即可保存。">
      <StepSection title="摘要">
        <div className="text-[13px] text-text-body space-y-1">
          <div>
            {state.info.name || '未命名调查员'} · {state.info.playerName || state.info.name || '—'}
          </div>
          <div className="text-text-muted text-[12px]">
            {state.age} 岁 · {selectedOcc?.name ?? '未选择职业'}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-1.5 mt-3">
          {ruleset.attributes.map((a) => (
            <div key={a.key} className="bg-panel rounded px-2 py-1.5 text-center">
              <div className="text-[9px] text-text-dim">{a.key}</div>
              <div className="text-[13px] font-bold font-mono text-text-primary">{state.attr[a.key] ?? '—'}</div>
            </div>
          ))}
        </div>
        <div className="flex gap-2 mt-3">
          {[
            { label: 'HP', value: derived.hp, color: '#4a8a4a' },
            { label: 'SAN', value: derived.san, color: '#7050a0' },
            { label: 'MP', value: derived.mp, color: '#4a7098' },
            { label: 'DB', value: derived.db, color: '#b8976a' },
            { label: 'MOV', value: derived.move, color: '#c08050' },
          ].map((pill) => (
            <div key={pill.label} className="flex-1 bg-panel rounded-md px-2 py-1.5 text-center">
              <div className="text-[9px] text-text-muted font-semibold">{pill.label}</div>
              <div className="text-[14px] font-bold font-mono" style={{ color: pill.color }}>
                {pill.value}
              </div>
            </div>
          ))}
        </div>
      </StepSection>

      <StepSection title="校验">
        {!preview || preview.validation.length === 0 ? (
          <div className="px-3 py-2 bg-[#eef6ea] text-[#3a7a3a] text-[12px] rounded-[6px]">看起来没问题</div>
        ) : (
          <div className="space-y-1.5">
            {preview.validation.map((issue, i) => (
              <div key={i} className="px-3 py-2 bg-[#fdecec] text-[#c04040] text-[12px] rounded-[6px]">
                {issue.message}
              </div>
            ))}
          </div>
        )}
      </StepSection>

      {mainSkills.length > 0 && (
        <StepSection title="主要技能">
          <div className="flex flex-wrap gap-1.5">
            {mainSkills.map((s) => (
              <span key={s.id} className="px-2.5 py-1 text-[11px] rounded-full bg-panel border border-border-light text-text-body">
                {s.name} {s.value}
              </span>
            ))}
          </div>
        </StepSection>
      )}
    </StepShell>
  )
}
