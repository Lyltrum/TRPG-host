import { useEffect, useReducer, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useCharacterStore, type CompletedCharacter } from '@/stores/character-store'
import { useRoomStore } from '@/stores/room-store'
import { useRuleset } from '@/hooks/useRuleset'
import { WIZARD_STEPS } from './wizard-steps'
import { DEFAULT_AGE, createInitialWizardState, wizardReducer, type WizardState } from './wizard-state'
import { useWizardPreview } from './useWizardPreview'
import { useWizardHydration } from './useWizardHydration'
import { useWizardSubmit } from './useWizardSubmit'
import { buildSkillComputeMap, pointBuyAttributes, stepBlockers, totalPointsRemaining } from './wizard-selectors'
import { ConceptStep } from './steps/ConceptStep'
import { AttributesStep } from './steps/AttributesStep'
import { AgeStep } from './steps/AgeStep'
import { OccupationStep } from './steps/OccupationStep'
import { OccupationPointsStep } from './steps/OccupationPointsStep'
import { InterestPointsStep } from './steps/InterestPointsStep'
import { BackgroundStep } from './steps/BackgroundStep'
import { FinishStep } from './steps/FinishStep'

/** 本地缓存的已建过的角色卡（比如从人物卡准备页点"编辑"回来）用来预填初始
 * 状态，避免每次进向导都从空白表单重新开始——后端水合到位后会覆盖这些值
 * （见 useWizardHydration），这里只是给"还没水合完成"这段时间一个更友好的
 * 起点。 */
function buildInitialState(existing: CompletedCharacter | null): WizardState {
  const base = createInitialWizardState()
  if (!existing) return base
  return {
    ...base,
    info: {
      name: existing.info.name,
      playerName: existing.info.playerName,
      gender: existing.info.gender,
      residence: existing.info.residence,
      birthplace: existing.info.birthplace,
    },
    age: existing.info.age ? Number(existing.info.age) || DEFAULT_AGE : DEFAULT_AGE,
    attr: { ...existing.attr },
    occupationId: existing.info.occupationId,
    skillAlloc: { ...existing.skillAlloc },
    equipment: existing.equipment,
    background: existing.background,
    notes: existing.notes,
    backgroundDetail: existing.backgroundDetail ?? base.backgroundDetail,
  }
}

/** 建卡向导的壳：header 进度条 / 步骤分发 / footer 上一步下一步 / 门禁提示
 * （重制设计 v2 §7）。 */
export default function CharacterWizardPage() {
  const navigate = useNavigate()
  const { ruleset, loading: rulesetLoading, error: rulesetError } = useRuleset()
  const roomId = useRoomStore((s) => s.roomId)

  const existingCharacter = useCharacterStore.getState().getForRoom(useRoomStore.getState().roomId ?? '')
  const [state, dispatch] = useReducer(wizardReducer, existingCharacter, buildInitialState)

  const { preview, previewError, pendingDelta } = useWizardPreview(ruleset, state)
  useWizardHydration(ruleset, dispatch)

  // ruleset 到达后，把缺失的点数购买属性补上默认值——只补 pointBuy=true 的
  // 属性，幸运不在其列（此前这里连幸运也一起填了默认值，会让它看起来"已经
  // 掷过"，见重制设计 v2 诊断表第 5 条）。
  useEffect(() => {
    if (!ruleset?.attributePointBuy) return
    const defaultValue = ruleset.attributePointBuy.defaultValue
    const defaults = Object.fromEntries(pointBuyAttributes(ruleset).map((a) => [a.key, defaultValue]))
    dispatch({ type: 'FILL_ATTR_DEFAULTS', defaults })
  }, [ruleset])

  const skillComputeMap = buildSkillComputeMap(preview)
  const selectedOccName = ruleset?.occupations.find((o) => o.id === state.occupationId)?.name ?? null
  const { handleSubmit, submitting, submitError } = useWizardSubmit(ruleset, state, skillComputeMap, selectedOccName)

  const [pendingConfirm, setPendingConfirm] = useState(false)
  useEffect(() => setPendingConfirm(false), [state.step])

  if (rulesetLoading) {
    return (
      <div className="animate-screen-in min-h-screen bg-page flex flex-col items-center justify-center px-5 text-center">
        <p className="text-sm text-text-muted">正在加载规则数据…</p>
      </div>
    )
  }
  if (rulesetError || !ruleset) {
    return (
      <div className="animate-screen-in min-h-screen bg-page flex flex-col items-center justify-center px-5 text-center gap-3">
        <p className="text-sm text-[#c04040]">{rulesetError || '规则数据加载失败'}</p>
        <button
          onClick={() => navigate(-1)}
          className="px-5 py-2.5 rounded-sm bg-card border border-border-light text-text-body text-sm font-semibold"
        >
          返回
        </button>
      </div>
    )
  }

  const stepMeta = WIZARD_STEPS[state.step]
  const blockers = stepBlockers(stepMeta.id, state, { ruleset, preview, pendingDelta })
  const isSoftGateStep = stepMeta.id === 'occPoints' || stepMeta.id === 'intPoints'
  const remaining = totalPointsRemaining(preview, pendingDelta)
  const isLastStep = state.step === WIZARD_STEPS.length - 1
  // 硬门禁：preview.validation 里的规则校验（比如"非职业技能已用114点兴趣
  // 点，超过预算110"）此前只在下方渲染成提示文字，从没被"下一步"/"完成
  // 创建"检查过——两池合计剩余可以是正数（职业池有富余）而兴趣池自己已经
  // 超支，此时旧逻辑会把它当成"还有剩余点数"的软提醒放行，真人实测撞见。
  // preview.validation 非空就必须硬拦，不管当前在哪一步。
  const hasValidationIssues = (preview?.validation.length ?? 0) > 0

  const goPrev = () => {
    if (state.step > 0) dispatch({ type: 'SET_STEP', step: state.step - 1 })
    else navigate(-1)
  }

  const goNext = () => {
    if (isLastStep) {
      if (hasValidationIssues) return
      void handleSubmit()
      return
    }
    if (blockers.length > 0) return
    if (isSoftGateStep) {
      if (hasValidationIssues) return
      if (remaining > 0 && !pendingConfirm) {
        setPendingConfirm(true)
        return
      }
    }
    setPendingConfirm(false)
    dispatch({ type: 'SET_STEP', step: state.step + 1 })
  }

  const nextDisabled =
    submitting ||
    (blockers.length > 0 && !isSoftGateStep) ||
    ((isSoftGateStep || isLastStep) && hasValidationIssues)
  const nextLabel = submitting
    ? '提交中…'
    : (isSoftGateStep || isLastStep) && hasValidationIssues
      ? '请先解决上方的超支问题'
      : isSoftGateStep && pendingConfirm
        ? `还剩 ${remaining} 点，确定继续？`
        : isLastStep
          ? '完成创建'
          : '下一步'

  return (
    <div className="animate-screen-in min-h-screen bg-page">
      <div className="sticky top-0 z-10 bg-page pt-1 pb-0">
        <div className="flex items-center gap-2.5 px-5 pt-0.5">
          <button
            onClick={goPrev}
            className="w-[34px] h-[34px] rounded-full bg-card border border-border-light flex items-center justify-center flex-shrink-0 active:bg-panel active:scale-[0.94] transition-all"
          >
            <ArrowLeft className="w-[18px] h-[18px] text-text-muted" strokeWidth={2.5} />
          </button>
          <h2 className="text-lg font-bold text-text-primary">创建角色</h2>
        </div>
        <div className="flex gap-1.5 px-5 py-3">
          {WIZARD_STEPS.map((s, i) => (
            <div
              key={s.id}
              className={`flex-1 h-[3px] rounded-[99px] transition-all duration-300 ${
                i < state.step ? 'bg-brass-dark' : i === state.step ? 'bg-brass' : 'bg-border-light'
              }`}
            />
          ))}
        </div>
        {!roomId && (
          <div className="mx-5 mb-3 px-3.5 py-2.5 bg-[#fdf3e0] border border-[#e0c088] rounded-[6px] text-[12px] text-[#8a6a2a]">
            当前未加入房间，创建的角色不会被保存。请先返回创建或加入一个房间。
          </div>
        )}
      </div>

      {stepMeta.id === 'concept' && <ConceptStep state={state} dispatch={dispatch} ruleset={ruleset} />}
      {stepMeta.id === 'attrs' && (
        <AttributesStep state={state} dispatch={dispatch} ruleset={ruleset} preview={preview} previewError={previewError} />
      )}
      {stepMeta.id === 'age' && <AgeStep state={state} dispatch={dispatch} ruleset={ruleset} preview={preview} />}
      {stepMeta.id === 'occupation' && <OccupationStep state={state} dispatch={dispatch} ruleset={ruleset} preview={preview} />}
      {stepMeta.id === 'occPoints' && (
        <OccupationPointsStep
          state={state}
          dispatch={dispatch}
          ruleset={ruleset}
          preview={preview}
          pendingDelta={pendingDelta}
          previewError={previewError}
        />
      )}
      {stepMeta.id === 'intPoints' && (
        <InterestPointsStep
          state={state}
          dispatch={dispatch}
          ruleset={ruleset}
          preview={preview}
          pendingDelta={pendingDelta}
          previewError={previewError}
        />
      )}
      {stepMeta.id === 'background' && <BackgroundStep state={state} dispatch={dispatch} />}
      {stepMeta.id === 'finish' && <FinishStep state={state} ruleset={ruleset} preview={preview} />}

      <div className="fixed bottom-0 left-0 right-0 bg-page border-t border-border-light px-5 py-3 max-w-[430px] mx-auto z-20">
        {submitError && <p className="text-[11px] text-[#c04040] text-center mb-2">{submitError}</p>}
        {stepMeta.id !== 'finish' && (preview?.validation.length ?? 0) > 0 && (
          <div className="mb-2 space-y-1">
            {preview!.validation.map((issue, i) => (
              <p key={i} className="text-[11px] text-[#c04040] text-center">
                ⚠️ {issue.message}
              </p>
            ))}
          </div>
        )}
        {blockers.length > 0 && !isSoftGateStep && (
          <p className="text-[11px] text-[#8a6a2a] text-center mb-2">{blockers[0]}</p>
        )}
        <div className="flex gap-2.5">
          <button
            onClick={goPrev}
            className="flex-1 flex items-center justify-center gap-1.5 px-5 py-3 rounded-sm text-sm font-semibold transition-all border border-border-mid bg-card text-text-body active:bg-panel active:scale-[0.97]"
          >
            上一步
          </button>
          <button
            onClick={goNext}
            disabled={nextDisabled}
            className="flex-1 flex items-center justify-center gap-1.5 px-5 py-3 rounded-sm text-sm font-semibold transition-all bg-brass text-white active:bg-brass-dark active:scale-[0.97] disabled:opacity-60"
          >
            {nextLabel} →
          </button>
        </div>
      </div>
    </div>
  )
}
