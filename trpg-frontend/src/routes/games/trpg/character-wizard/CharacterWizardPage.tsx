import { useEffect, useReducer, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useCharacterStore, type CompletedCharacter } from '@/stores/character-store'
import { useRoomStore } from '@/stores/room-store'
import { useRuleset } from '@/hooks/useRuleset'
import { quickBuildCharacter } from '@/services/character/character-api'
import { translateCharacterValidationError } from '@/services/character/ruleset-api'
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

  const existingCharacter = useCharacterStore
    .getState()
    .getForRoom(useRoomStore.getState().roomId ?? '', useRoomStore.getState().playerId)
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

  // 一键生成（真人实测反馈：八步向导对新人不友好）。数值全部由后端生成并
  // 直接落成完成态，前端只负责把名字送过去、拿到结果跳走——**不复用向导的
  // 提交流程**：那条路要先把一屋子本地状态凑齐才能提交，而这条路根本没有
  // 本地状态。跳转前不写 character-store，让 ready 页按 roomId 从后端水合。
  const [quickBuilding, setQuickBuilding] = useState(false)
  const [quickBuildError, setQuickBuildError] = useState('')
  const handleQuickBuild = async () => {
    if (!roomId) {
      setQuickBuildError('房间信息丢失，请重新创建/加入房间')
      return
    }
    setQuickBuilding(true)
    setQuickBuildError('')
    try {
      const characterId = await quickBuildCharacter(roomId, state.info.name)
      useRoomStore.getState().setCharacterId(characterId)
      navigate('/room/ready')
    } catch (err) {
      setQuickBuildError(translateCharacterValidationError(err))
    } finally {
      setQuickBuilding(false)
    }
  }

  // 🔴 `bg-card` 是桌面木色，不能少：木纹是 multiply 混合，底下没颜色等于没铺。
  const deskClass = 'theme-coc desk-grain desk-lamp desk-sigil bg-card'

  if (rulesetLoading) {
    return (
      <div className={`${deskClass} animate-screen-in min-h-full flex flex-col items-center justify-center px-5 text-center`}>
        <p className="relative z-10 text-[13px] text-text-body">正在加载规则数据…</p>
      </div>
    )
  }
  if (rulesetError || !ruleset) {
    return (
      <div className={`${deskClass} animate-screen-in min-h-full flex flex-col items-center justify-center px-5 text-center gap-3`}>
        <p className="relative z-10 text-[13px] text-rust">{rulesetError || '规则数据加载失败'}</p>
        <button
          onClick={() => navigate(-1)}
          className="cut-corner relative z-10 px-5 py-2.5 bg-input border border-border-mid text-text-body text-[13px] font-semibold"
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
    <div className={`${deskClass} animate-screen-in h-full flex flex-col relative overflow-hidden`}>
      <div className="relative z-10 flex items-center gap-2.5 px-4 pt-3.5">
        <button
          onClick={goPrev}
          className="cut-corner w-8 h-8 bg-input border border-border-mid flex items-center justify-center flex-shrink-0 active:bg-panel active:scale-[0.94] transition-all"
        >
          <ArrowLeft className="w-[18px] h-[18px] text-text-body" strokeWidth={2.5} />
        </button>
        <h2 className="text-[16px] font-bold text-text-primary tracking-[0.04em]">创建角色</h2>
      </div>

      {/* 🔴 进度 = 档案夹里的**分隔页**，双层错落（4 + 4）。
          单层平铺八个标签时每个只有 43px 宽、字要压到 9px——低于中文小字
          10.5px 下限（那条判据是被"太阳底下看不清"逼出来的），不为排版破例。
          错落也是真实档案夹的做法：不错开标签会互相遮挡。 */}
      <div className="relative z-10 px-3 pt-2.5">
        {[WIZARD_STEPS.slice(0, 4), WIZARD_STEPS.slice(4)].map((group, groupIndex) => (
          <div key={groupIndex} className="flex gap-[3px] mt-[2px] first:mt-0">
            {group.map((s) => {
              const index = WIZARD_STEPS.indexOf(s)
              const isCurrent = index === state.step
              return (
                <div
                  key={s.id}
                  className={`tab-flap flex-1 text-center text-[10.5px] text-ink transition-all ${
                    isCurrent
                      ? 'bg-dossier font-bold pt-[7px] pb-[5px]'
                      : index < state.step
                        ? 'bg-[#a8926a] pt-[5px] pb-1'
                        : // 还没走到的那几片纸压得更暗，但**不能压过头**：墨色字
                          // 压在 #7b6a50 上只有 3.06:1，卡在最弱一档的下限上。
                          'bg-[#8d7a5c] pt-[5px] pb-1'
                  }`}
                >
                  {s.short}
                </div>
              )
            })}
          </div>
        ))}
      </div>

      {!roomId && (
        <div className="relative z-10 mx-3 mt-2 px-2.5 py-1.5 bg-input border border-brass-dark text-[10.5px] text-brass-bright leading-relaxed">
          当前未加入房间，创建的角色不会被保存。请先返回创建或加入一个房间。
        </div>
      )}

      {/* 表单纸：向导正文。`theme-paper` 把语义 token 显式改回浅色——CSS 变量
          沿 DOM 继承，只是"不加 theme-coc"不够，祖先上有就照样生效。
          `--paper` 给 <StepSection> 的标签用来咬断框线。 */}
      <div
        className="theme-paper paper-grain relative z-10 mx-3 mt-2 flex-1 min-h-0 flex flex-col bg-dossier text-ink shadow-[0_1px_0_rgba(0,0,0,.34),0_10px_16px_-8px_rgba(0,0,0,.6)]"
        style={{ ['--paper' as string]: '#cbb894' }}
      >
        <div className="flex-none flex items-baseline gap-2 px-3.5 pt-3 pb-1 mb-2 mx-0 border-b-[1.5px] border-ink/40">
          <span className="text-[14.5px] font-bold tracking-[0.04em] text-ink">{stepMeta.title}</span>
          <span className="typed ml-auto text-[10.5px] text-ink-soft">
            {state.step + 1} / {WIZARD_STEPS.length}
          </span>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto px-3.5 pb-4">

      {stepMeta.id === 'concept' && (
        <ConceptStep
          state={state}
          dispatch={dispatch}
          ruleset={ruleset}
          onQuickBuild={() => void handleQuickBuild()}
          quickBuilding={quickBuilding}
          quickBuildError={quickBuildError}
        />
      )}
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
        </div>
      </div>

      <div className="relative z-10 flex-none px-3 pt-2 pb-3 mt-2 bg-page border-t border-border-mid">
        {submitError && <p className="text-[10.5px] text-rust text-center mb-1.5">{submitError}</p>}
        {stepMeta.id !== 'finish' && (preview?.validation.length ?? 0) > 0 && (
          <div className="mb-1.5 space-y-0.5">
            {preview!.validation.map((issue, i) => (
              <p key={i} className="text-[10.5px] text-rust text-center leading-relaxed">
                ⚠️ {issue.message}
              </p>
            ))}
          </div>
        )}
        {blockers.length > 0 && !isSoftGateStep && (
          <p className="text-[10.5px] text-brass-bright text-center mb-1.5">{blockers[0]}</p>
        )}
        <div className="flex gap-2.5">
          <button
            onClick={goPrev}
            className="flex-1 py-3 text-[13.5px] font-bold tracking-[0.08em] transition-all border border-border-mid bg-input text-text-body active:bg-panel"
          >
            上一步
          </button>
          {/* 🔴 「下一步」有五种文案（下一步 / 完成创建 / 提交中… / 请先解决上方
              的超支问题 / 还剩 N 点，确定继续？）。后三种明显长，字号跟着缩一档，
              否则会被挤成两行把按钮撑高。 */}
          <button
            onClick={goNext}
            disabled={nextDisabled}
            className={`flex-1 py-3 font-bold transition-all ${
              nextDisabled
                ? 'bg-panel border border-border-mid text-text-dim text-[11.5px]'
                : 'seal bg-brass-dark border border-brass text-text-primary text-[13.5px] tracking-[0.08em] active:translate-y-[1px]'
            }`}
          >
            {nextLabel}
            {!nextDisabled && ' →'}
          </button>
        </div>
      </div>
    </div>
  )
}
