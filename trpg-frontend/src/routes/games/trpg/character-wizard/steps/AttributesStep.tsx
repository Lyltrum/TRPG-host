import { useState } from 'react'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { BookOpen, Brain, Eye, Heart, Lightbulb, Maximize2, Shield, Zap } from 'lucide-react'
import { useRoomStore } from '@/stores/room-store'
import { rollAttributePool, rollAttributes, rollLuck } from '@/services/character/character-api'
import { friendlyErrorMessage } from '@/services/api-client'
import { StepShell, StepSection } from '../components/StepShell'
import { PoolBar } from '../components/PoolBar'
import { AttributeAllocCard } from '../components/AttributeAllocCard'
import { LuckCard } from '../components/LuckCard'
import { ensureCharacterId } from '../wizard-network'
import {
  attrBudget,
  attrPointsTotal,
  attrRange,
  computeEvenDistribution,
  normalizeDerivedStats,
  pointBuyAttributes,
} from '../wizard-selectors'
import type { WizardAction, WizardState } from '../wizard-state'

const ATTR_ICONS: Record<string, typeof Heart> = {
  STR: Shield,
  CON: Heart,
  POW: Brain,
  DEX: Zap,
  APP: Eye,
  SIZ: Maximize2,
  INT: Lightbulb,
  EDU: BookOpen,
}

const ATTR_COLORS: Record<string, string> = {
  STR: '#c04040',
  CON: '#c08050',
  POW: '#7050a0',
  DEX: '#4a8a4a',
  APP: '#8a4070',
  SIZ: '#b8976a',
  INT: '#4a7098',
  EDU: '#6a6050',
}

const ATTR_MEANINGS: Record<string, string> = {
  STR: '影响近战伤害与负重',
  CON: '影响生命值上限',
  POW: '影响魔法值与理智起始值',
  DEX: '影响先攻与闪避基础值',
  APP: '影响他人对你的第一印象',
  SIZ: '影响生命值与伤害加值',
  INT: '影响灵感检定与兴趣技能点数',
  EDU: '影响知识类技能起始值',
}

const PRESETS = [40, 50, 60, 70]

/** 步 1 · 属性与幸运（重制设计 v2 §6-步骤1）：生成方式 / 分配 / 幸运 / 衍生值四节。 */
export function AttributesStep({
  state,
  dispatch,
  ruleset,
  preview,
  previewError,
}: {
  state: WizardState
  dispatch: (action: WizardAction) => void
  ruleset: Ruleset
  preview: CharacterComputeResult | null
  previewError: string
}) {
  const [generationBusy, setGenerationBusy] = useState(false)
  const [generationError, setGenerationError] = useState('')
  const [luckBusy, setLuckBusy] = useState(false)
  const [luckError, setLuckError] = useState('')
  const [confirmReroll, setConfirmReroll] = useState<'roll' | 'roll_pool' | null>(null)

  const attrs = pointBuyAttributes(ruleset)
  const { min: attrMin, max: attrMax } = attrRange(ruleset, state.generationMethod)
  const budget = attrBudget(ruleset, state.generationMethod, state.attributePoolTotal)
  const total = attrPointsTotal(state.attr, attrs)
  const attrEditable = state.generationMethod !== 'roll'
  const roomId = useRoomStore((s) => s.roomId)
  const hasRolledOnce = state.generationMethod !== 'pointbuy' || state.poolRolls.length > 0

  const sumOther = (key: string) => attrs.reduce((sum, a) => (a.key === key ? sum : sum + (state.attr[a.key] ?? 0)), 0)

  const doRollAttributes = async () => {
    if (!roomId) {
      setGenerationError('请先创建/加入房间，才能使用服务端掷骰')
      return
    }
    setGenerationBusy(true)
    setGenerationError('')
    try {
      const characterId = await ensureCharacterId(roomId)
      const result = await rollAttributes(roomId, characterId)
      dispatch({
        type: 'ROLL_ATTRIBUTES_SUCCESS',
        attributes: result.attributes,
        attrInputs: Object.fromEntries(attrs.map((a) => [a.key, String(result.attributes[a.key] ?? '')])),
      })
    } catch (err) {
      setGenerationError(friendlyErrorMessage(err, '掷骰失败'))
    } finally {
      setGenerationBusy(false)
    }
  }

  const doRollPool = async () => {
    if (!roomId) {
      setGenerationError('请先创建/加入房间，才能使用掷点池')
      return
    }
    setGenerationBusy(true)
    setGenerationError('')
    try {
      const characterId = await ensureCharacterId(roomId)
      const result = await rollAttributePool(roomId, characterId)
      const resetAttr = Object.fromEntries(attrs.map((a) => [a.key, attrMin]))
      dispatch({
        type: 'ROLL_POOL_SUCCESS',
        rolls: result.rolls,
        total: result.total,
        resetAttr,
        resetAttrInputs: Object.fromEntries(Object.entries(resetAttr).map(([k, v]) => [k, String(v)])),
      })
    } catch (err) {
      setGenerationError(friendlyErrorMessage(err, '掷点池失败'))
    } finally {
      setGenerationBusy(false)
    }
  }

  const handlePickMethod = (method: 'pointbuy' | 'roll' | 'roll_pool') => {
    if (method === 'pointbuy') {
      dispatch({ type: 'SET_GENERATION_METHOD', method: 'pointbuy' })
      return
    }
    if (hasRolledOnce) {
      setConfirmReroll(method)
      return
    }
    void (method === 'roll' ? doRollAttributes() : doRollPool())
  }

  const confirmRerollNow = () => {
    const method = confirmReroll
    setConfirmReroll(null)
    if (!method) return
    void (method === 'roll' ? doRollAttributes() : doRollPool())
  }

  const handleAdjust = (key: string, delta: number) => {
    if (!attrEditable) return
    const newVal = Math.max(attrMin, Math.min(attrMax, (state.attr[key] ?? 0) + delta))
    if (delta > 0 && sumOther(key) + newVal > budget) return
    dispatch({ type: 'SET_ATTR_VALUE', key, value: newVal })
  }

  const handleSetValue = (key: string, value: number) => {
    if (!attrEditable) return
    const clamped = Math.max(attrMin, Math.min(attrMax, value))
    if (sumOther(key) + clamped > budget) return
    dispatch({ type: 'SET_ATTR_VALUE', key, value: clamped })
  }

  const handleInputCommit = (key: string) => {
    if (!attrEditable) return
    const raw = parseInt(state.attrInputs[key] ?? '', 10)
    const maxAllowed = Math.min(attrMax, budget - sumOther(key))
    const clamped = Number.isNaN(raw) ? (state.attr[key] ?? attrMin) : Math.max(attrMin, Math.min(maxAllowed, raw))
    dispatch({ type: 'SET_ATTR_VALUE', key, value: clamped })
  }

  const handleEvenDistribute = () => {
    if (!attrEditable) return
    const patch = computeEvenDistribution(
      budget,
      attrMin,
      attrMax,
      attrs.map((a) => a.key)
    )
    dispatch({ type: 'SET_ATTR_BULK', patch })
  }

  const handleClear = () => {
    if (!attrEditable) return
    const resetTo = state.generationMethod === 'roll_pool' ? attrMin : (ruleset.attributePointBuy?.defaultValue ?? attrMin)
    const patch = Object.fromEntries(attrs.map((a) => [a.key, resetTo]))
    dispatch({ type: 'SET_ATTR_BULK', patch })
  }

  const doRollLuck = async () => {
    if (!roomId) {
      setLuckError('请先创建/加入房间，才能掷幸运')
      return
    }
    setLuckBusy(true)
    setLuckError('')
    try {
      const characterId = await ensureCharacterId(roomId)
      const result = await rollLuck(roomId, characterId)
      dispatch({ type: 'SET_LUCK_ROLL', luckRoll: result })
    } catch (err) {
      setLuckError(friendlyErrorMessage(err, '掷幸运失败'))
    } finally {
      setLuckBusy(false)
    }
  }

  const derived = normalizeDerivedStats(preview?.derivedStats)
  const ageStale = state.ageResult != null && !state.ageApplied

  return (
    <StepShell title="属性与幸运" lead="先决定属性怎么来，再分配到八项属性上，最后单独掷一次幸运。">
      {ageStale && (
        <div className="px-3.5 py-2.5 bg-[#fdf3e0] border border-[#e0c088] rounded-[6px] text-[12px] text-[#8a6a2a]">
          改属性会取消已套用的年龄调整，之后要回第 3 步重新套用。
        </div>
      )}

      <StepSection
        title="生成方式"
        tip={
          state.generationMethod === 'roll'
            ? '属性已由服务端掷骰生成，下方仅供查看，不可手动调整。'
            : state.generationMethod === 'roll_pool' && state.attributePoolTotal != null
              ? `已掷出总点数 ${state.attributePoolTotal}，请手动分配到下方八项属性。`
              : '默认按点数购买法手动分配；也可以改用服务端掷骰或掷点池。'
        }
      >
        <div className="grid grid-cols-3 gap-2">
          <button
            onClick={() => handlePickMethod('pointbuy')}
            disabled={generationBusy}
            className={`py-2 text-[12px] font-semibold rounded-[6px] transition-all disabled:opacity-50 ${
              state.generationMethod === 'pointbuy' ? 'bg-brass text-white' : 'bg-input border border-border-light text-text-muted'
            }`}
          >
            点数购买
          </button>
          <button
            onClick={() => handlePickMethod('roll')}
            disabled={generationBusy || !roomId}
            className={`py-2 text-[12px] font-semibold rounded-[6px] transition-all disabled:opacity-50 ${
              state.generationMethod === 'roll' ? 'bg-brass text-white' : 'bg-input border border-border-light text-text-muted'
            }`}
          >
            {generationBusy ? '掷骰中…' : '服务端掷骰'}
          </button>
          <button
            onClick={() => handlePickMethod('roll_pool')}
            disabled={generationBusy || !roomId}
            className={`py-2 text-[12px] font-semibold rounded-[6px] transition-all disabled:opacity-50 ${
              state.generationMethod === 'roll_pool' ? 'bg-brass text-white' : 'bg-input border border-border-light text-text-muted'
            }`}
          >
            {generationBusy ? '掷骰中…' : '掷点池'}
          </button>
        </div>
        {confirmReroll && (
          <div className="mt-2.5 px-3 py-2.5 bg-[#fdf3e0] border border-[#e0c088] rounded-[6px]">
            <p className="text-[11px] text-[#8a6a2a] mb-2">重新掷点会清空已分配的属性，并作废已套用的年龄调整。</p>
            <div className="flex gap-2">
              <button
                onClick={() => setConfirmReroll(null)}
                className="flex-1 py-1.5 text-[11px] font-semibold rounded-[6px] border border-border-mid bg-card text-text-body"
              >
                取消
              </button>
              <button onClick={confirmRerollNow} className="flex-1 py-1.5 text-[11px] font-semibold rounded-[6px] bg-brass text-white">
                确认重掷
              </button>
            </div>
          </div>
        )}
        {generationError && <p className="text-[11px] text-[#c04040] mt-2">{generationError}</p>}
        {state.generationMethod === 'roll_pool' && state.poolRolls.length > 0 && (
          <div className="mt-2.5 grid grid-cols-2 gap-1.5">
            {state.poolRolls.map((r, i) => (
              <div key={i} className="text-[10px] text-text-dim font-mono bg-panel rounded px-2 py-1">
                {r.kind} [{r.dice.join(',')}] = {r.value}
              </div>
            ))}
          </div>
        )}
      </StepSection>

      {state.generationMethod !== 'roll' && (
        <div className="sticky top-[54px] z-10">
          <PoolBar
            label="属性总点数"
            spent={total}
            budget={budget}
            exactMatchRequired={state.generationMethod === 'roll_pool'}
          />
        </div>
      )}

      <StepSection title="分配" tip="可以先「平均分配」，再用 +/− 按角色想法微调。">
        {attrEditable && (
          <div className="grid grid-cols-2 gap-2 mb-3">
            <button
              onClick={handleEvenDistribute}
              className="py-2 text-[12px] font-semibold rounded-[6px] border border-border-mid bg-card text-text-body active:bg-panel transition-all"
            >
              平均分配
            </button>
            <button
              onClick={handleClear}
              className="py-2 text-[12px] font-semibold rounded-[6px] border border-border-mid bg-card text-text-body active:bg-panel transition-all"
            >
              清空
            </button>
          </div>
        )}
        <div className="space-y-2">
          {attrs.map((attribute) => {
            const key = attribute.key
            const value = state.attr[key] ?? 0
            return (
              <AttributeAllocCard
                key={key}
                attrKey={key}
                label={attribute.label}
                icon={ATTR_ICONS[key] ?? Shield}
                color={ATTR_COLORS[key] ?? '#b8976a'}
                value={value}
                inputValue={state.attrInputs[key] ?? String(value)}
                min={attrMin}
                max={attrMax}
                step5Only={state.generationMethod === 'roll_pool'}
                presets={attrEditable ? PRESETS : []}
                presetDisabled={(p) => sumOther(key) + p > budget}
                editable={attrEditable}
                meaning={ATTR_MEANINGS[key]}
                onAdjust={(delta) => handleAdjust(key, delta)}
                onSetValue={(v) => handleSetValue(key, v)}
                onInputChange={(raw) => dispatch({ type: 'SET_ATTR_INPUT', key, value: raw })}
                onInputCommit={() => handleInputCommit(key)}
              />
            )
          })}
        </div>
      </StepSection>

      <StepSection title="幸运">
        <LuckCard
          value={state.attr.LUCK ?? null}
          luckRoll={state.luckRoll}
          rolling={luckBusy}
          error={luckError}
          autoGenerated={state.generationMethod === 'roll'}
          onRoll={() => void doRollLuck()}
        />
      </StepSection>

      <StepSection title="衍生属性">
        {previewError && <p className="text-[11px] text-[#c04040] mb-2">{previewError}</p>}
        <div className="flex gap-2">
          {[
            { label: 'HP', value: `${derived.hp}`, color: '#4a8a4a' },
            { label: 'SAN', value: `${derived.san}`, color: '#7050a0' },
            { label: 'MP', value: `${derived.mp}`, color: '#4a7098' },
            { label: 'DB', value: derived.db, color: '#b8976a' },
            { label: 'MOV', value: `${derived.move}`, color: '#c08050' },
          ].map((pill) => (
            <div key={pill.label} className="flex-1 bg-panel rounded-md px-2.5 py-2 text-center">
              <div className="text-[10px] text-text-muted font-semibold">{pill.label}</div>
              <div className="text-[16px] font-bold font-mono" style={{ color: pill.color }}>
                {pill.value}
              </div>
            </div>
          ))}
        </div>
      </StepSection>
    </StepShell>
  )
}
