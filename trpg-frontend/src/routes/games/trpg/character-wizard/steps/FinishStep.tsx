import { useMemo } from 'react'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { StepShell, StepSection } from '../components/StepShell'
import { effectiveAttr, normalizeDerivedStats } from '../wizard-selectors'
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
  const attrs = effectiveAttr(state)

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
    <StepShell lead="确认一下角色摘要，看看有没有校验提示，点击下方「完成创建」即可保存。">
      <StepSection title="摘要">
        <div className="text-[13.5px] font-bold text-ink">
          {state.info.name || '未命名调查员'} · {state.info.playerName || state.info.name || '—'}
        </div>
        <div className="text-[11.5px] text-ink-soft mb-2">
          {state.age} 岁 · {selectedOcc?.name ?? '未选择职业'}
        </div>
        <div className="grid grid-cols-3 gap-1">
          {ruleset.attributes.map((a) => (
            <div key={a.key} className="border border-ink/28 bg-white/15 px-2 py-1 text-center">
              <div className="typed text-[10.5px] text-ink-soft">{a.key}</div>
              <div className="text-[13px] font-bold font-mono text-ink">{attrs[a.key] ?? '—'}</div>
            </div>
          ))}
        </div>
        <div className="flex gap-1 mt-2">
          {[
            { label: 'HP', value: derived.hp, color: '#3d6b2f' },
            { label: 'SAN', value: derived.san, color: '#57407e' },
            { label: 'MP', value: derived.mp, color: '#3a5a7a' },
            { label: 'DB', value: derived.db, color: '#5c461e' },
            { label: 'MOV', value: derived.move, color: '#7a4a28' },
          ].map((pill) => (
            <div key={pill.label} className="flex-1 border border-ink/28 bg-white/15 px-1 py-1 text-center">
              <div className="typed text-[10.5px] text-ink-soft">{pill.label}</div>
              <div className="text-[14px] font-bold font-mono" style={{ color: pill.color }}>
                {pill.value}
              </div>
            </div>
          ))}
        </div>
      </StepSection>

      <StepSection title="校验">
        {/* 通过 = 一枚盖上去的绿章；失败 = 逐条锈红。两种状态形状不同，
            不只是颜色不同——一眼就能分出"过了"还是"没过"。 */}
        {!preview || preview.validation.length === 0 ? (
          <div className="text-center py-1">
            <span className="stamped typed inline-block text-[11px] font-bold px-2.5 py-1 text-[#3d6b2f]">
              看起来没问题
            </span>
          </div>
        ) : (
          <div className="space-y-1">
            {preview.validation.map((issue, i) => (
              <div
                key={i}
                className="px-2.5 py-1.5 border-l-[3px] border-l-rust-dark border border-ink/25 bg-white/20 text-rust-dark text-[11.5px] leading-relaxed"
              >
                {issue.message}
              </div>
            ))}
          </div>
        )}
      </StepSection>

      {mainSkills.length > 0 && (
        <StepSection title="主要技能">
          <div className="flex flex-wrap gap-1">
            {mainSkills.map((s) => (
              <span
                key={s.id}
                className="px-2 py-0.5 text-[10.5px] border border-ink/30 bg-white/20 text-ink"
              >
                {s.name} {s.value}
              </span>
            ))}
          </div>
        </StepSection>
      )}
    </StepShell>
  )
}
