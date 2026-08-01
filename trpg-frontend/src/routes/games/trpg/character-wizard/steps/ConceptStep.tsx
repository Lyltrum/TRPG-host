import { Wand2 } from 'lucide-react'
import type { Ruleset } from 'trpg-sdk'
import { StepShell, StepSection } from '../components/StepShell'
import type { WizardAction, WizardState } from '../wizard-state'

/** 步 0 · 基本信息（重制设计 v2 §6-步骤0）。 */
export function ConceptStep({
  state,
  dispatch,
  onQuickBuild,
  quickBuilding,
  quickBuildError,
}: {
  state: WizardState
  dispatch: (action: WizardAction) => void
  ruleset: Ruleset
  /** 一键生成（零基础玩家的第二条路）。名字仍然要玩家自己填。 */
  onQuickBuild: () => void
  quickBuilding: boolean
  quickBuildError: string
}) {
  const { info } = state
  const canQuickBuild = info.name.trim().length > 0 && !quickBuilding
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

      {/* 零基础玩家的第二条路（真人实测反馈：八步向导对新人不友好）。
          放在第一步、名字输入框正下方——这是新人唯一确定会看到的一屏。 */}
      <StepSection title="不想一步步来？">
        <p className="text-[12px] text-text-muted mb-2.5">
          填好上面的角色姓名，点这里由系统随机生成一张完整合法的调查员卡
          <span className="text-brass-dark">（含一段属于他的过去）</span>，直接开局。
          想自己捏就继续走下面的「下一步」。
        </p>
        <button
          onClick={onQuickBuild}
          disabled={!canQuickBuild}
          className={`w-full flex items-center justify-center gap-1.5 px-5 py-2.5 rounded-sm text-[13px] font-semibold transition-all ${
            canQuickBuild
              ? 'bg-card border border-brass text-brass-dark active:bg-brass active:text-white active:scale-[0.97]'
              : 'bg-panel border border-border-light text-text-dim cursor-not-allowed'
          }`}
        >
          <Wand2 className="w-4 h-4" />
          {quickBuilding ? '生成中…' : '一键生成一张角色卡'}
        </button>
        {/* 🔴 真人实测：这一步同步等 7–9.5 秒（背景那次 LLM 调用），而按钮上
            只有"生成中…"三个字——新人会以为卡住了。说清在等什么，顺带让他知道
            这条路的产出里有背景故事（exec/25 P1 #4）。 */}
        {quickBuilding && (
          <p className="text-[11px] text-text-muted text-center mt-2 leading-[1.6]">
            正在掷属性、分配技能，
            <br />
            并为你的调查员写一段过去，大约十秒。
          </p>
        )}
        {!info.name.trim() && !quickBuilding && (
          <p className="text-[11px] text-text-dim text-center mt-2">请先填写角色姓名</p>
        )}
        {quickBuildError && (
          <p className="text-[11px] text-[#c04040] text-center mt-2">{quickBuildError}</p>
        )}
      </StepSection>
    </StepShell>
  )
}
