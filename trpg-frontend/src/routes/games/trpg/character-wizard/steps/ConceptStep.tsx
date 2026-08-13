import { useState } from 'react'
import { createPortal } from 'react-dom'
import { BookUser, Wand2, X } from 'lucide-react'
import type { CharacterTemplate, Ruleset } from 'trpg-sdk'
import { templateSubtitle } from '@/services/character/character-view'
import { StepShell, StepSection } from '../components/StepShell'
import type { WizardAction, WizardState } from '../wizard-state'

/** 步 0 · 基本信息（重制设计 v2 §6-步骤0）。 */
export function ConceptStep({
  state,
  dispatch,
  onQuickBuild,
  quickBuilding,
  quickBuildError,
  templates,
  onUseTemplate,
  usingTemplate,
  templateError,
}: {
  state: WizardState
  dispatch: (action: WizardAction) => void
  ruleset: Ruleset
  /** 一键生成（零基础玩家的第二条路）。名字仍然要玩家自己填。 */
  onQuickBuild: () => void
  quickBuilding: boolean
  quickBuildError: string
  /** 我的常用卡（第三条路）。空数组 = 卡库是空的或没登录，整块不渲染。 */
  templates: CharacterTemplate[]
  onUseTemplate: (templateId: string) => void
  usingTemplate: boolean
  templateError: string
}) {
  const { info } = state
  const [pickerOpen, setPickerOpen] = useState(false)
  const canQuickBuild = info.name.trim().length > 0 && !quickBuilding
  return (
    <StepShell lead="先给你的调查员起个名字，其余信息随时可以回来改。">
      <StepSection title="调查员信息">
        <div className="space-y-3">
          <input
            value={info.name}
            onChange={(e) => dispatch({ type: 'SET_INFO', patch: { name: e.target.value } })}
            placeholder="角色姓名"
            className="wz-field w-full px-3 py-2 text-[14px]"
          />
          <input
            value={info.playerName}
            onChange={(e) => dispatch({ type: 'SET_INFO', patch: { playerName: e.target.value } })}
            placeholder="玩家名（可选，默认同角色姓名）"
            className="wz-field w-full px-3 py-2 text-[14px]"
          />
          <div>
            <label className="text-[11px] font-medium text-text-muted mb-1 block">性别</label>
            <select
              value={info.gender}
              onChange={(e) => dispatch({ type: 'SET_INFO', patch: { gender: e.target.value } })}
              className="wz-field w-full px-3 py-2 text-[14px]"
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
                className="wz-field w-full px-3 py-2 text-[14px]"
              />
            </div>
            <div>
              <label className="text-[11px] font-medium text-text-muted mb-1 block">出生地</label>
              <input
                value={info.birthplace}
                onChange={(e) => dispatch({ type: 'SET_INFO', patch: { birthplace: e.target.value } })}
                className="wz-field w-full px-3 py-2 text-[14px]"
              />
            </div>
          </div>
        </div>
      </StepSection>

      {/* 零基础玩家的第二条路（真人实测反馈：八步向导对新人不友好）。
          放在第一步、名字输入框正下方——这是新人唯一确定会看到的一屏。 */}
      <StepSection title="不想一步步来？">
        <p className="text-[11.5px] text-ink-soft mb-2.5">
          填好上面的角色姓名，点这里由系统随机生成一张完整合法的调查员卡
          <span className="text-brass-dark">（含一段属于他的过去）</span>，直接开局。
          想自己捏就继续走下面的「下一步」。
        </p>
        <button
          onClick={onQuickBuild}
          disabled={!canQuickBuild}
          className={`cut-corner w-full flex items-center justify-center gap-1.5 px-5 py-2.5 text-[13px] font-semibold transition-all ${
            canQuickBuild
              ? 'border border-brass-dark text-brass-dark bg-white/25 active:bg-brass-dark active:text-dossier active:scale-[0.97]'
              : 'border border-ink/25 text-ink-soft cursor-not-allowed'
          }`}
        >
          <Wand2 className="w-4 h-4" />
          {quickBuilding ? '生成中…' : '一键生成一张角色卡'}
        </button>
        {/* 🔴 真人实测：这一步同步等 7–9.5 秒（背景那次 LLM 调用），而按钮上
            只有"生成中…"三个字——新人会以为卡住了。说清在等什么，顺带让他知道
            这条路的产出里有背景故事（exec/25 P1 #4）。 */}
        {quickBuilding && (
          <p className="text-[10.5px] text-ink-soft text-center mt-2 leading-[1.6]">
            正在掷属性、分配技能，
            <br />
            并为你的调查员写一段过去，大约十秒。
          </p>
        )}
        {!info.name.trim() && !quickBuilding && (
          <p className="text-[10.5px] text-ink-soft text-center mt-2">请先填写角色姓名</p>
        )}
        {quickBuildError && (
          <p className="text-[10.5px] text-rust-dark text-center mt-2">{quickBuildError}</p>
        )}
      </StepSection>

      {/* 第三条路：我带了自己的调查员来。
          🔴 **只放一个入口，不平铺**：卡库会越攒越多，而这一屏的主任务是填
          基本信息（真机反馈 2026-08-13：两张卡就占了大半屏）。列表放进浮层。
          🔴 卡库为空时整块不渲染——新玩家第一次进来这里什么都没有，摆一个
          空入口只会让他以为哪里没加载出来。 */}
      {templates.length > 0 && (
        <StepSection title="用我的常用卡">
          <p className="text-[11.5px] text-ink-soft mb-2.5">
            以前存进卡库的调查员。选一张会
            <span className="text-brass-dark">复制一份新的</span>
            过来，这一局怎么玩都不会改到卡库里那张。
          </p>
          <button
            onClick={() => setPickerOpen(true)}
            disabled={usingTemplate}
            className={`cut-corner w-full flex items-center justify-center gap-1.5 px-5 py-2.5 text-[13px] font-semibold transition-all ${
              usingTemplate
                ? 'border border-ink/25 text-ink-soft cursor-not-allowed'
                : 'border border-brass-dark text-brass-dark bg-white/25 active:bg-brass-dark active:text-dossier active:scale-[0.97]'
            }`}
          >
            <BookUser className="w-4 h-4" />
            {usingTemplate ? '取用中…' : `从我的调查员里选（${templates.length}）`}
          </button>
          {templateError && (
            <p className="text-[10.5px] text-rust-dark text-center mt-2">{templateError}</p>
          )}
        </StepSection>
      )}

      {pickerOpen && (
        <TemplatePicker
          templates={templates}
          disabled={usingTemplate}
          onPick={(id) => {
            setPickerOpen(false)
            onUseTemplate(id)
          }}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </StepShell>
  )
}

/**
 * 从卡库里挑一张的浮层。
 *
 * 🔴 用底部浮层而不是把列表铺在步骤里：卡库条目数没有上限，而建卡第一步是
 * 玩家**必经**的一屏，它的主任务是填基本信息。浮层的高度是自己的事，撑不到
 * 那一屏（形状照 `CharacterReadyPage` 的 InvestigatorSheet）。
 *
 * 🔴 **必须 portal 到 `#root`**（2026-08-13 真人反馈：卡库里两张卡只看得见
 * 一张，浮层被拦腰截断）。别的浮层（InviteSheet / InvestigatorSheet）都挂在
 * 页面根部，只有这一个长在**建卡向导滚动纸张容器的内部**——而
 * `position: fixed` 一旦祖先链上有 `transform`/`filter`/`contain`，就改成锚定
 * 那个祖先，于是浮层被裁在纸张的下边缘而不是手机屏底部（`#root` 自己正是靠
 * `transform: translateZ(0)` 当手机屏的包含块，见 styles.css 那段注释）。
 * portal 出去是**结构性**保证：不依赖"祖先链上恰好没有人建包含块"。
 */
function TemplatePicker({
  templates,
  disabled,
  onPick,
  onClose,
}: {
  templates: CharacterTemplate[]
  disabled: boolean
  onPick: (templateId: string) => void
  onClose: () => void
}) {
  return createPortal(
    <div className="fixed inset-0 z-40 flex items-end justify-center">
      <div className="absolute inset-0 bg-black/60 animate-fade-in" onClick={onClose} />
      {/* 🔴 `theme-paper`：CSS 变量沿 DOM 继承，光"不加 theme-coc"不够——
          祖先上有就照样生效，症状是牛皮纸上一片空白。
          高度用 `max-h-[74%]`（相对手机屏）而不是 `74vh`：桌面预览里手机框是
          固定 820px 的一块，`vh` 量的却是真实浏览器窗口，两者对不上。 */}
      <div
        className="theme-paper paper-grain relative w-full max-h-[74%] bg-dossier text-ink flex flex-col animate-slide-up max-w-[430px] shadow-[0_-1px_0_rgba(255,255,255,.22),0_-3px_10px_rgba(0,0,0,.35)] border-t-[3px] border-brass-dark"
      >
        <span className="tab-flap absolute left-[26px] -top-[19px] typed text-[10.5px] px-3.5 pt-[5px] pb-1 bg-brass-dark text-dossier">
          我的调查员
        </span>

        {/* 收起键自己占一行、钉在顶部：绝对定位的控件不知道内容多长，必然撞 */}
        <div className="flex justify-end px-3 pt-3 pb-1 flex-none">
          <button
            onClick={onClose}
            aria-label="关闭"
            className="w-[30px] h-[30px] flex items-center justify-center border border-ink/30 bg-white/25 active:scale-[0.94] transition-all"
          >
            <X className="w-4 h-4 text-ink-soft" />
          </button>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto px-4 pb-6 space-y-2">
          {templates.map((template) => {
            return (
              <button
                key={template.templateId}
                onClick={() => onPick(template.templateId)}
                disabled={disabled}
                className="cut-corner w-full flex items-center gap-2 px-4 py-3 text-left border border-brass-dark/60 text-ink bg-white/25 active:bg-brass-dark active:text-dossier active:scale-[0.97] transition-all disabled:opacity-50"
              >
                <BookUser className="w-4 h-4 shrink-0 text-brass-dark" />
                <span className="flex-1 min-w-0">
                  <span className="block text-[13px] font-semibold truncate">{template.name}</span>
                  <span className="block text-[10.5px] text-ink-soft truncate">
                    {templateSubtitle(template)}
                  </span>
                </span>
              </button>
            )
          })}
        </div>
      </div>
    </div>,
    document.getElementById('root') ?? document.body
  )
}
