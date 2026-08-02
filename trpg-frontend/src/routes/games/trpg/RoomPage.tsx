import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Users, Map, BookOpen, ScrollText, Star, X, SendHorizontal, Plus, Save, FlagOff, Heart, Mic, MessagesSquare, Scroll, EyeOff } from 'lucide-react'
import { useState, useRef, useEffect, useCallback, type FormEvent } from 'react'
import type { ChatMessage, PartyCharacter } from 'trpg-sdk'
import { useRoomStore } from '@/stores/room-store'
import { useGameStore } from '@/stores/game-store'
import { useAuthStore } from '@/stores/auth-store'
import { useCharacterStore } from '@/stores/character-store'
import { connectWebSocket, waitForWsOpen, sdk, onWsMessage, disconnectWebSocket, friendlyErrorMessage } from '@/services/api-client'
import { BACKGROUND_DETAIL_FIELDS } from '@/data/character-model'
import { endGame } from '@/services/room'
import { fetchCharacter } from '@/services/character/character-api'
import { toCompletedCharacter } from '@/services/character/character-view'
import { useRoomPlayers } from '@/hooks/useRoomPlayers'
import { useRuleset } from '@/hooks/useRuleset'
import { useSpeechInput } from '@/hooks/useSpeechInput'

// ─── Types ───────────────────────────────────────────
interface Message {
  type: 'system' | 'narr' | 'player' | 'dice'
  sender?: string
  content: string
  time: string
  isSelf?: boolean
  // 只给你一个人的私密结果（exec/18 ⑥/②）。**线下同桌时旁人看得见你的屏幕**，
  // 私密的物理前提本来就不成立——折叠成"点按查看"是那种场合唯一的补救。
  private?: boolean
}

// 两段式玩家掷骰（feat/keeper-agent）：守秘人裁决"需要检定"后不直接掷骰，
// 而是随叙事一起推一张"待掷"卡片，玩家点击确认后骰值才由服务端权威生成。
interface PendingCheck {
  id: string
  kind: 'skill' | 'san'
  skill: string | null
  rolling: boolean
}

// 地图结构化数据未接线：不展示假地点，避免砸信任（设计评测 P1）

const DICE_OPTIONS = [
  { id: 'd100', label: 'D100' },
  { id: 'd20', label: 'D20' },
  { id: 'd6', label: 'D6' },
] as const

type DiceType = typeof DICE_OPTIONS[number]['id']

const DIFFICULTY_COLORS: Record<string, string> = {
  crit: '#5aaa5a',
  success: '#4a8a4a',
  fail: '#d45050',
  fumble: '#d45050',
}

// ─── Panel Component ─────────────────────────────────
// heightVh：不传就是原来的"按内容自适应、最多 72vh"；传了就固定成这个高度
// （不再随内容多少变化），配合内部 overflow-y-auto 滚动——用于内容量本身
// 会因为切页签/切分类而差很多、又不想让面板跟着一起忽高忽低的场景。
/** 🔴 每一份档案要有**自己的身份**，不能五个面板长得一模一样。
 *
 * 真人反馈：「每块内容都该有各自的特色，一眼就知道是哪一模块」。
 * 解法不用另起炉灶——**真实档案柜本来就是靠彩色标签舌分类的**：
 *   `accent` 决定标签舌与顶边的索引色，`paper` 给每份档案略不同的纸色。
 * 加上各自已有的结构特征（测绘纸网格 / 横格纸 / 量表刻度 / 档案条），
 * 打开任意一份，第一眼就知道是哪一份。
 */
function BottomPanel({ open, onClose, title, children, heightVh, accent = '#8a6a2e', paper = '#cbb894' }: { open: boolean; onClose: () => void; title: string; children: React.ReactNode; heightVh?: number; accent?: string; paper?: string }) {
  useEffect(() => {
    if (open) document.body.style.overflow = 'hidden'
    else document.body.style.overflow = ''
    return () => { document.body.style.overflow = '' }
  }, [open])

  const maxH = heightVh ?? 72

  return (
    <>
      {open && <div className="fixed inset-0 z-40 bg-black/60" onClick={onClose} />}
      {/* 🔴 面板 = 档案：牛皮纸 + 标签舌 + 装订孔。不是 iOS 的圆角抽屉，
          所以没有顶部那根"拖拽小横杠"——档案不靠拖，靠标签舌认。
          `flap` 是从档案顶部探出来的那一小片，写着这份档案叫什么。 */}
      <div
        // 🔴 `theme-paper`：把 token **显式改回浅色**。
        // 只是"不加 theme-coc"不够——CSS 变量沿 DOM 继承，本面板是 RoomPage
        // 根节点的后代，祖先上的 theme-coc 照样生效。真机症状是牛皮纸上一片
        // 空白（浅奶白的字压浅纸）。判据：**在深色页面里放纸，必须挂 theme-paper。**
        // 🔴 **不要大范围投影**（原来是 `0 -14px 34px rgba(0,0,0,.66)`）：
        // 它在面板下缘糊出一大团黑，真机上看就是"纸的底部烂了"。
        // 纸压在桌上只需要**一条上缘亮线 + 一条紧贴的暗线**，两条 1px 就够。
        className={`theme-paper paper-grain fixed bottom-0 left-0 right-0 z-50 text-ink shadow-[0_-1px_0_rgba(255,255,255,.22),0_-3px_10px_rgba(0,0,0,.35)] transition-transform duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] max-w-[430px] mx-auto overflow-hidden ${open ? 'translate-y-0' : 'translate-y-full'}`}
        style={{
          backgroundColor: paper,
          // 栏目标签要用纸色去"咬断"边框线（真实表单的做法），所以把纸色
          // 暴露成变量给 <Section> 用。
          ['--paper' as string]: paper,
          ['--accent' as string]: accent,
          borderTop: `3px solid ${accent}`,
          ...(heightVh ? { height: `${maxH}vh` } : { maxHeight: `${maxH}vh` }),
        }}
      >
        {/* 🔴 标签舌只在展开时渲染。它是 `-top-[19px]` 探出面板上缘的，面板
            收起时被 translate-y-full 推下去，标签舌正好卡在屏幕底边**一直露着**
            ——真机上五个面板的舌头全叠在输入框下面。 */}
        {open && (
          <>
            <button
              onClick={onClose}
              className="tab-flap absolute left-[26px] -top-[19px] z-10 typed text-[9px] px-3.5 pt-[5px] pb-1"
              style={{ backgroundColor: accent, color: paper }}
            >
              {title}
            </button>
            {/* 装订孔：孔里透出底下桌面的暗。正文左边距要避开它 */}
            <span className="punch absolute left-[13px] top-[46px] w-[11px] h-[11px] rounded-full" />
            <span className="punch absolute left-[13px] top-[74px] w-[11px] h-[11px] rounded-full" />
            <span className="punch absolute left-[13px] top-[102px] w-[11px] h-[11px] rounded-full" />
          </>
        )}
        <div
          className="relative overflow-y-auto pl-[34px] pr-4 pb-6"
          style={{ maxHeight: `calc(${maxH}vh - 8px)` }}
        >
          {/* 🔴 收起键放在**流里**，不是 absolute 浮在内容上。
              浮的那版跟每一页的第一行都打架（角色卡的分类标签、队友的人数行…）
              ——绝对定位的控件没有"内容会长什么样"的信息，必然撞。
              这里它自己占一行，任何页面都不会被它压住。 */}
          <div className="flex justify-end pt-2.5 pb-1.5">
            <button
              onClick={onClose}
              className="typed flex items-center gap-1 text-[9px] text-ink/45 active:text-ink px-1 py-0.5"
            >
              收起
              <X className="w-3 h-3" strokeWidth={2.5} />
            </button>
          </div>
          {children}
        </div>
      </div>
    </>
  )
}

/** 档案里的**栏目**。
 *
 * 🔴 真人反馈：同一个面板里「装备 / 背景故事 / 备注 / 细节」内容性质完全不同，
 * 却长得一模一样（小标题 + 一段字，堆成流水线）。**内容不同，皮就该不同。**
 *
 * 四种皮，一眼可辨：
 *   `form`  表单栏 —— 浅内底 + 实线框。数值、地点这类填空
 *   `prose` 打字报告 —— 左侧一道粗墨条 + 略深的纸。长文本
 *   `note`  手写便条 —— 横格纸 + 虚线框。备注、批注
 *   `list`  清单 —— 点线框。装备这类逐条列的东西
 *
 * 栏目名做成压在框线上的小标签（真实表单就是这么印的），标签底色用面板的纸色
 * 把框线"咬断"——所以 BottomPanel 要把 `--paper` 暴露出来。
 */
function Section({
  label,
  tone = 'form',
  children,
}: {
  label: string
  tone?: 'form' | 'prose' | 'note' | 'list'
  children: React.ReactNode
}) {
  const skin = {
    form: 'bg-black/[0.045] border border-ink/25',
    prose: 'bg-black/[0.075] border-y border-r border-ink/15 border-l-[3px] border-l-ink/55',
    note: 'notepaper border border-dashed border-ink/40',
    list: 'border border-dotted border-ink/40',
  }[tone]
  return (
    <div className={`relative mt-[18px] px-3 pt-3.5 pb-3 ${skin}`}>
      <span
        className="typed absolute -top-[7px] left-2.5 px-1.5 text-[8.5px] text-ink/75"
        style={{ backgroundColor: 'var(--paper)' }}
      >
        {label}
      </span>
      {children}
    </div>
  )
}

/** 空字段：显式画成"这里是空的"，不是一句灰色的话。 */
function Blank({ children }: { children: React.ReactNode }) {
  return (
    <p className="typed text-[10px] text-ink/35 text-center py-1.5 border border-dashed border-ink/25">
      {children}
    </p>
  )
}

/** 回形针：夹在玩家便签左上角的金属丝。
 *
 * 🔴 画成真的双回环丝形，不是一根小横杠——它是"这是一张便签"最强的信号。
 * 两层描边叠出金属感：粗的一层是本体，细的一层是高光。
 *
 * 🔴 自己与队友用不同金属（黄铜 / 钢），跟纸色、名字后缀一起构成三重区分
 * ——多人局里一眼分辨靠的是颜色，不是读名字。
 */
function Paperclip({ metal }: { metal: 'brass' | 'steel' }) {
  const [body, shine] = metal === 'brass' ? ['#8a6a2e', '#e8c885'] : ['#9aa0a8', '#e8ecf1']
  const d = 'M8 40V13a5.5 5.5 0 0 1 11 0v25a4.6 4.6 0 0 1-9.2 0V18a2.7 2.7 0 0 1 5.4 0v21'
  return (
    <span className="clip" aria-hidden="true">
      <svg viewBox="0 0 26 46" fill="none" className="w-full h-full block">
        <path d={d} stroke={body} strokeWidth="2.6" strokeLinecap="round" />
        <path d={d} stroke={shine} strokeWidth="0.9" strokeLinecap="round" opacity="0.7" />
      </svg>
    </span>
  )
}

// ─── Dice System ─────────────────────────────────────
// 跟角色卡/技能那几个面板一样用 BottomPanel（底部弹层，不盖满整个屏幕），
// 不再是独立的全屏深色页面。面板现在跟其他面板一样常驻挂载、靠 open 控制
// 滑入滑出，所以每次重新打开都要把上一次投骰的结果清空，不然会看到上一轮
// 的结果还留着。
function DiceModal({ open, onClose, onResult }: { open: boolean; onClose: () => void; onResult: (result: number, diceType: DiceType) => void }) {
  const [diceType, setDiceType] = useState<DiceType>('d100')
  const [shakeLevel, setShakeLevel] = useState(0)
  const [result, setResult] = useState<number | null>(null)
  const [rolling, setRolling] = useState(false)
  const [showResult, setShowResult] = useState(false)
  const [tens, setTens] = useState(0)
  const [ones, setOnes] = useState(0)
  const tableRef = useRef<HTMLDivElement>(null)
  const isGrabbed = useRef(false)
  const directionChanges = useRef(0)
  const lastDirX = useRef(0)
  const lastDirY = useRef(0)

  useEffect(() => {
    if (open) {
      setDiceType('d100')
      setResult(null)
      setShowResult(false)
      setRolling(false)
      setShakeLevel(0)
    }
  }, [open])

  const roll = (power: number) => {
    setRolling(true)
    setShowResult(false)

    let finalResult: number
    let t = 0, o = 0

    if (diceType === 'd100') {
      t = Math.floor(Math.random() * 10)
      o = Math.floor(Math.random() * 10)
      finalResult = t * 10 + o
      if (finalResult === 0) finalResult = 100
      setTens(t)
      setOnes(o)
    } else if (diceType === 'd20') {
      finalResult = Math.floor(Math.random() * 20) + 1
    } else {
      finalResult = Math.floor(Math.random() * 6) + 1
    }

    const dur = 500 + power * 100
    setTimeout(() => {
      setResult(finalResult)
      setShowResult(true)
      setRolling(false)
    }, dur)
  }

  const handleMouseDown = () => {
    if (rolling || showResult) return
    isGrabbed.current = true
    directionChanges.current = 0
    lastDirX.current = 0
    lastDirY.current = 0
    setShakeLevel(0)
  }

  const handleMouseMove = (e: React.MouseEvent | React.TouchEvent) => {
    if (!isGrabbed.current) return
    const clientX = 'touches' in e ? e.touches[0].clientX : e.clientX
    const clientY = 'touches' in e ? e.touches[0].clientY : e.clientY

    if (tableRef.current) {
      const rect = tableRef.current.getBoundingClientRect()
      const dx = clientX - (rect.left + rect.width / 2)
      const dy = clientY - (rect.top + rect.height / 2)
      const dirX = Math.sign(dx)
      const dirY = Math.sign(dy)

      if (lastDirX.current !== 0 && dirX !== lastDirX.current) directionChanges.current++
      if (lastDirY.current !== 0 && dirY !== lastDirY.current) directionChanges.current++
      lastDirX.current = dirX
      lastDirY.current = dirY

      const level = Math.min(5, Math.floor(directionChanges.current / 2.5))
      setShakeLevel(level)
    }
  }

  const handleMouseUp = () => {
    if (!isGrabbed.current) return
    isGrabbed.current = false
    if (shakeLevel >= 1) {
      roll(shakeLevel)
    } else {
      roll(1)
    }
  }

  const confirmResult = () => {
    if (result === null) return
    onResult(result, diceType)
    onClose()
  }

  const renderDiceDisplay = () => {
    const glow = rolling ? 'opacity-40' : ''
    return (
      <div ref={tableRef} className={`relative w-full h-48 flex items-center justify-center select-none ${isGrabbed.current ? 'cursor-grabbing' : 'cursor-grab'} ${glow}`}>
        {diceType === 'd100' ? (
          <div className="flex items-center gap-6">
            <div className="text-center">
              <div className={`text-[42px] font-bold font-mono tracking-wider ${tens === 0 ? 'text-[#c8c0b8]' : 'text-[#eeead8]'} transition-colors`}>
                {String(tens * 10).padStart(2, '0')}
              </div>
              <div className="text-[10px] text-[#9088a0] mt-1 font-mono">十位</div>
            </div>
            <div className="text-[28px] text-[#9088a0] font-mono">+</div>
            <div className="text-center">
              <div className={`text-[42px] font-bold font-mono ${ones === 0 ? 'text-[#c8c0b8]' : 'text-[#eeead8]'} transition-colors`}>
                {ones}
              </div>
              <div className="text-[10px] text-[#9088a0] mt-1 font-mono">个位</div>
            </div>
          </div>
        ) : (
          <div
            className={`text-[64px] font-bold font-mono text-[#eeead8] ${isGrabbed.current ? 'scale-105' : ''} transition-transform duration-150`}
            style={{
              clipPath: diceType === 'd20' ? 'polygon(50% 0%, 95% 25%, 95% 75%, 50% 100%, 5% 75%, 5% 25%)' : undefined,
              background: 'linear-gradient(145deg, #2a2630, #1a1620)',
              width: diceType === 'd20' ? '90px' : '80px',
              height: diceType === 'd20' ? '96px' : '80px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              borderRadius: diceType === 'd6' ? '12px' : undefined,
              border: '1px solid rgba(255,255,255,0.08)',
            }}
          >
            {rolling ? (diceType === 'd20' ? Math.floor(Math.random() * 20) + 1 : Math.floor(Math.random() * 6) + 1) : result || '-'}
          </div>
        )}
      </div>
    )
  }

  const getVerdict = (): { label: string; color: string } | null => {
    if (result === null || diceType !== 'd100') return null
    const skill = 65
    if (result <= 5) return { label: '极限成功', color: DIFFICULTY_COLORS.crit }
    if (result <= 33) return { label: '困难成功', color: DIFFICULTY_COLORS.success }
    if (result <= skill) return { label: '成功', color: DIFFICULTY_COLORS.success }
    return { label: '失败', color: DIFFICULTY_COLORS.fail }
  }

  const verdict = getVerdict()

  return (
    <BottomPanel open={open} onClose={onClose} title="骰子检定">
      {/* Dice type selector */}
      <div className="flex gap-1.5 mb-3.5">
        {DICE_OPTIONS.map((opt) => (
          <button
            key={opt.id}
            onClick={() => { if (!rolling) { setDiceType(opt.id); setResult(null); setShowResult(false); setShakeLevel(0) } }}
            className={`flex-1 text-center text-[12px] font-semibold py-1.5 rounded-[99px] border transition-all ${
              diceType === opt.id ? 'bg-brass text-white border-brass' : 'bg-panel text-text-muted border-border-light'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* Dice context info */}
      <div className="text-center mb-3">
        <span className="text-xs text-brass-dark font-semibold bg-brass/10 px-4 py-1 rounded-full inline-block">
          侦察
        </span>
        <div className="font-mono text-xs text-text-muted mt-1">
          {diceType === 'd100' ? '目标: 65 · D% = 十位 + 个位' : '自由检定'}
        </div>
      </div>

      {/* Dice table——保留深色"赌桌"质感，作为浅色面板里的一个独立区块，
          不再是撑满整个屏幕的深色页面 */}
      <div
        className="rounded-md bg-[#1a1620] px-4 pt-5 pb-4 flex flex-col items-center relative overflow-hidden"
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        onTouchStart={handleMouseDown}
        onTouchMove={handleMouseMove}
        onTouchEnd={handleMouseUp}
      >
        {/* Shake glow ring */}
        {shakeLevel >= 2 && !rolling && !showResult && (
          <div
            className="absolute w-52 h-52 rounded-full pointer-events-none transition-all duration-200"
            style={{
              background: `radial-gradient(circle, rgba(184,151,106,${0.04 + shakeLevel * 0.04}) 0%, transparent 70%)`,
              transform: `scale(${1 + shakeLevel * 0.05})`,
            }}
          />
        )}

        {renderDiceDisplay()}

        {!rolling && !showResult && (
          <div className="text-center mt-2">
            <span className="text-xs text-[#9088a0]">
              {shakeLevel === 0 ? '👆 按住这里来回拖动 · 摇动后松手' :
               shakeLevel <= 2 ? '⚡ 再用力一点……' :
               shakeLevel <= 4 ? '🔥 快了！' :
               '💥 松手投出！'}
            </span>
          </div>
        )}

        {/* Shake meter */}
        {!rolling && !showResult && (
          <div className="flex gap-1 mt-3">
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className={`w-6 h-1 rounded-full transition-all duration-200 ${
                i < shakeLevel ? (i >= 3 ? 'bg-brass' : 'bg-[rgba(184,151,106,0.5)]') : 'bg-[rgba(255,255,255,0.08)]'
              }`} />
            ))}
          </div>
        )}

        {rolling && (
          <div className="text-center mt-2 text-xs text-[#9088a0] animate-pulse">
            🎲 骰子飞出去了……
          </div>
        )}
      </div>

      {/* Result */}
      {showResult && result !== null && (
        <div className="flex flex-col items-center pt-4 gap-3 animate-[fadeIn_0.3s_ease]">
          <div className="text-center">
            {diceType === 'd100' ? (
              <>
                <div className="flex items-center justify-center gap-2 text-text-dim font-mono text-sm">
                  <span>{String(tens * 10).padStart(2, '0')}</span>
                  <span>+</span>
                  <span>{ones}</span>
                  <span>=</span>
                </div>
                <div className={`text-[44px] font-bold font-mono ${result <= 5 ? 'text-[#5aaa5a]' : result > 65 ? 'text-[#d45050]' : 'text-[#4a8a4a]'}`}>
                  {String(result).padStart(2, '0')}
                </div>
              </>
            ) : (
              <div className="text-[44px] font-bold font-mono text-text-primary">{result}</div>
            )}
            {verdict && (
              <div className="text-base font-bold mt-1" style={{ color: verdict.color }}>{verdict.label}</div>
            )}
            <div className="text-xs text-text-dim mt-1 font-mono">
              {diceType === 'd100' ? `侦察 65% · 需求 ≤65` : `${diceType.toUpperCase()} · 自由检定`}
            </div>
          </div>

          <button
            onClick={confirmResult}
            className="w-full py-3 rounded-sm bg-brass text-white text-sm font-semibold active:bg-brass-dark active:scale-[0.97] transition-all"
          >
            确认并发送
          </button>
        </div>
      )}
    </BottomPanel>
  )
}

// ─── Main RoomPage ───────────────────────────────────
export default function RoomPage() {
  const navigate = useNavigate()
  const roomId = useRoomStore((s) => s.roomId)
  const roomCode = useRoomStore((s) => s.roomCode)
  const playerId = useRoomStore((s) => s.playerId)
  const reconnectToken = useRoomStore((s) => s.reconnectToken)
  const nickname = useAuthStore((s) => s.nickname)
  const { ruleset } = useRuleset()
  // 🔴 局内角色卡以**后端**为准（issue #96 加了读接口，但此前只有准备页接上，
  // 聊天室一直读 localStorage）。本地缓存只作拉回来之前的首屏占位。
  // 这么改同时解决两件事：
  //   ① 同一浏览器两个标签页进同一房间时不再串卡（各自按自己的 characterId 拉）；
  //   ② HP/SAN 不再靠广播增量补写——掉线期间错过的广播以前永远补不回来，现在
  //      每次变动后重拉一次，数值总是权威值。
  const characterId = useRoomStore((s) => s.characterId)
  const cachedCharacter = useCharacterStore((s) =>
    roomId ? s.getForRoom(roomId, playerId) : null
  )
  const [remoteCharacter, setRemoteCharacter] = useState<typeof cachedCharacter>(null)
  const character = remoteCharacter ?? cachedCharacter
  // 🔴 下面这几个值**只在回调内部用于渲染**，绝不能进 WS 订阅 / replay 兜底
  // 那两个 effect 的依赖数组。依赖一变，effect 就会退订重订阅（或把进行中的
  // replay 轮询 cancel 掉），**窗口期内到达的消息直接丢失**——真人实测复现：
  // 房主整局收不到开场旁白，玩家却收到了。
  //
  // 两个诱因：`senderName` 依赖角色卡（改从后端异步拉之后必然变化一次）、
  // `roomInfo` 来自轮询（每次返回都是新对象，等于每隔几秒重订阅一次）。
  // 后者是既有问题，只是角色卡改成异步后才被稳定复现出来。
  const characterRef = useRef(character)
  characterRef.current = character

  // 从后端重拉自己那张卡。进房拉一次；HP/SAN 被服务端改写后再拉一次。
  const reloadCharacter = useCallback(() => {
    if (!roomId || !characterId || !ruleset) return
    fetchCharacter(roomId, characterId)
      .then((saved) => setRemoteCharacter(toCompletedCharacter(saved, ruleset)))
      .catch(() => {
        // 拉不到不打断对局：继续用手上这份（首屏占位或上一次拉到的）。
      })
  }, [roomId, characterId, ruleset])

  const reloadCharacterRef = useRef(reloadCharacter)
  reloadCharacterRef.current = reloadCharacter

  useEffect(() => {
    reloadCharacter()
  }, [reloadCharacter])
  const roomInfo = useRoomPlayers(roomCode)
  const roomInfoRef = useRef(roomInfo)
  roomInfoRef.current = roomInfo
  // 🔴 自己的显示名要跟别人**同源**（真人实测 exec/23 #57）：刷新之后本地
  // 角色卡还没拉回来时，这里曾退回**账号昵称**，于是同一屏上守秘人叫「李明轩」、
  // 自己的气泡却写着「凌铭辉」。
  // 后端建完卡就把 `Player.nickname` 换成角色名了（#52），而房间预览每 3 秒
  // 轮询一次、必定带着它——拿它兜底比拿账号名兜底可靠得多，也不必等角色卡。
  const myRoomNickname = roomInfo?.players.find((p) => p.playerId === playerId)?.nickname
  const senderName = character?.info.name || myRoomNickname || nickname || '你'
  const senderNameRef = useRef(senderName)
  senderNameRef.current = senderName
  const isHost = roomInfo?.players.find((p) => p.playerId === playerId)?.isHost ?? false
  // 房主选模组时落在 game-store；访客/刷新后优先 room-store.moduleId（同 StoryPage 的取值口径）
  const roomModuleId = useRoomStore((s) => s.moduleId)
  const sceneModuleId = useGameStore((s) => s.sceneId)
  const moduleId = roomModuleId || sceneModuleId || null
  const [confirmEnd, setConfirmEnd] = useState(false)
  const [ending, setEnding] = useState(false)
  const [endError, setEndError] = useState('')
  const [confirmExit, setConfirmExit] = useState(false)
  const [messages, setMessages] = useState<Message[]>([])
  // 开场旁白常在 RoomPage 订阅 onWsMessage 之前就推完（房主点开始后立刻
  // navigate）；用 event id 去重，把 GET /replay 与实时 WS 合成一条时间线。
  const seenEventKeysRef = useRef<Set<string>>(new Set())
  // 去重闸门：见过返回 false（丢弃），没见过登记并返回 true（渲染）。
  // 🔴 `id` 缺失时**直接放行**（exec/19 #42）——身份不明就不该假装认识它，
  // 重复显示只是难看，静默丢弃是永久丢消息。
  const dedupe = (key: string, id?: string | null) => {
    if (!id) return true
    if (seenEventKeysRef.current.has(key)) return false
    seenEventKeysRef.current.add(key)
    return true
  }
  // 两个独立界面（issue #107）：「主持人」是跟 AI 守秘人的对话（全房间广播、
  // 进 AI 上下文），「讨论区」是玩家之间的商量（AI 完全看不见）。同一个输入框
  // 按当前频道分流到 action.submit / chat.send 两条通道。
  const [channel, setChannel] = useState<'dm' | 'chat'>('dm')
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  // 私密行动（exec/18 ⑥）：勾上后这一条只回给自己，同处的队友看不到。
  // 守秘人照常看得见——私密是玩家↔玩家，不是玩家↔KP。每发一条自动复位，
  // 不做成常驻开关：忘了关会把整局都变成私密，而玩家不会察觉。
  const [privateAction, setPrivateAction] = useState(false)
  const [typing, setTyping] = useState(false)
  const [openPanel, setOpenPanel] = useState<string | null>(null)
  const [sheetPage, setSheetPage] = useState<'info' | 'background'>('info')
  // 队友角色卡（exec/14 P5.3）：真人桌上卡是互相传阅的，此前系统只能读回
  // 自己那张，反而比真人桌封闭。展开哪一位由 expandedMemberId 决定。
  const [partyCharacters, setPartyCharacters] = useState<PartyCharacter[] | null>(null)
  const [expandedMemberId, setExpandedMemberId] = useState<string | null>(null)
  // 已点开的私密气泡下标。默认折叠，点一下才显形。
  const [revealedPrivate, setRevealedPrivate] = useState<Set<number>>(new Set())
  const [skillsTab, setSkillsTab] = useState<'occupation' | 'interest'>('occupation')
  const [showDice, setShowDice] = useState(false)
  const [pendingCheck, setPendingCheck] = useState<PendingCheck | null>(null)
  // 首次待掷检定教学（session 内一次）
  const [showDiceTip, setShowDiceTip] = useState(() => {
    try {
      return sessionStorage.getItem('aidm-dice-tip-seen') !== '1'
    } catch {
      return true
    }
  })
  const notesKey = roomId ? `aidm-notes-${roomId}` : null
  // ★ 之前"📋 案件笔记"标题是直接塞进 textarea 初始内容里的普通文本，用户
  // 一编辑/全选删除就会把标题本身也删掉。改成占位符（placeholder），真正
  // 的内容默认是空白，标题不会被误删，也不占用户还没写的正文空间。
  const [notes, setNotes] = useState(
    () => (notesKey && localStorage.getItem(notesKey)) || ''
  )
  const [lastSaved, setLastSaved] = useState<string | null>(() => (notesKey ? localStorage.getItem(notesKey) : null) ? new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : null)
  // 案件简报（player_intro）：建卡前的 StoryPage 展示过一次，进房后无处可查
  // （真人实测问题清单 #1）。在这里缓存一份，供「速记本」面板常驻展示。
  const [playerIntro, setPlayerIntro] = useState<string | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // ★ block: 'nearest' 很关键——默认的 scrollIntoView 会尝试把目标"居中"，
    // 这会一路把祖先链上所有能滚动的容器都滚一遍，包括 #root（虽然它设了
    // overflow:hidden，但那只是不让用户手动滚，程序仍然能改它的 scrollTop，
    // 一旦被带偏就会把整个 RoomPage 顶飞，见「继续游戏」跳转后的空白页 bug）。
    // 'nearest' 只调整真正需要滚的那个容器（消息列表自己），不会殃及无关祖先。
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [messages, chatMessages, channel])

  // ★ 访客走的是 /join → /character → /character-ready → /room，全程不经过
  // /lobby——而 connectWebSocket 之前只在 LobbyPage 里调用过，导致访客的浏览器
  // 从头到尾没建立过 WS 连接，发消息全部被静默丢弃（见 2026-07-13 多人测试报告
  // P0）。这里补一次同样的连接+room.join，对已经连过的房主是幂等空操作。
  useEffect(() => {
    if (!roomId || !playerId) return
    let cancelled = false
    const ws = connectWebSocket(roomId)
    waitForWsOpen(ws)
      .then(() => {
        if (cancelled) return
        sdk.roomSocket.joinRoom(playerId, { reconnectToken: reconnectToken || '', roomCode, nickname: nickname || '玩家' })
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [roomId, playerId, roomCode, nickname, reconnectToken])

  // player_intro 建卡前就已固定不变，只需要请求一次并缓存，不用跟着 WS 事件刷新。
  useEffect(() => {
    if (!moduleId) return
    let cancelled = false
    sdk.modules
      .getDetail(moduleId)
      .then((detail) => {
        if (!cancelled && detail.playerIntro) setPlayerIntro(detail.playerIntro)
      })
      .catch(() => { /* 拿不到就不展示这块区域，笔记面板照常可用 */ })
    return () => {
      cancelled = true
    }
  }, [moduleId])

  // 进房回补主持人时间线（开场旁白 + 已发生的行动/叙事），避免只靠 WS 漏消息。
  useEffect(() => {
    if (!roomId || !reconnectToken) return
    let cancelled = false
    const moduleId =
      useRoomStore.getState().moduleId || useGameStore.getState().sceneId || null
    const staleOpening =
      '案件已加载。守秘人整理好了开场的场景描述，故事即将开始……'

    ;(async () => {
      const boot: Message[] = [{
        type: 'system',
        content: '案件档案已加载 · 向守秘人描述你的行动即可推进',
        time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
      }]
      try {
        // game.start 的开场可能仍在生成：短轮询 replay，只认服务端 narration.push
        // （禁止前端再垫 openingScript，否则会与权威开场叠成两段）
        let events: Awaited<ReturnType<typeof sdk.rooms.getReplay>> = []
        for (let attempt = 0; attempt < 8; attempt++) {
          if (cancelled) return
          events = await sdk.rooms.getReplay(roomId, reconnectToken)
          if (events.some((e) => e.eventType === 'narration.push')) break
          await new Promise((r) => setTimeout(r, 400))
        }
        if (cancelled) return
        for (const ev of events) {
          const payload = (ev.payload ?? {}) as Record<string, unknown>
          const t = ev.createdAt
            ? new Date(ev.createdAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
            : ''
          if (ev.eventType === 'narration.push' && typeof payload.text === 'string') {
            let text = payload.text
            // 仅替换极旧占位句；不另注入第二段模组正文
            if (text === staleOpening && moduleId) {
              try {
                const detail = await sdk.modules.getDetail(moduleId)
                const real =
                  detail.openingScript ||
                  detail.playerIntro ||
                  detail.storyPages?.[0]
                if (real) text = real
              } catch { /* 拉不到就保留原文 */ }
            }
            if (!dedupe(`narr:${ev.id}`, ev.id)) continue
            boot.push({ type: 'narr', sender: '守秘人', content: text, time: t })
          } else if (ev.eventType === 'action.submit' && typeof payload.utterance === 'string') {
            if (!dedupe(`act:${ev.id}`, ev.id)) continue
            const isSelf = ev.playerId === playerId
            boot.push({
              type: 'player',
              sender: isSelf ? senderNameRef.current : '调查员',
              content: payload.utterance,
              time: t,
              isSelf,
            })
          }
        }
        // 仍无旁白：只显示等待提示，不客户端垫第二段开场
        if (!boot.some((m) => m.type === 'narr')) {
          boot.push({
            type: 'system',
            content: '守秘人正在开场…',
            time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
          })
          setTyping(true)
        }
      } catch {
        // replay 失败时至少给系统条
      }
      if (cancelled) return
      setMessages(boot)
      if (boot.some(m => m.type === 'narr')) setTyping(false)
    })()

    return () => {
      cancelled = true
    }
  }, [roomId, reconnectToken, playerId])

  // 服务端广播订阅：
  // - action.broadcast：任何玩家对守秘人说的**原话**。自己的那条也靠这条广播
  //   回显，不再本地乐观插入——所有人（包括自己）看到的时间线完全一致，这就是
  //   修"聊天记录像被隔离"bug 的方式。
  // - narration.push：守秘人的叙事回复（全房间）。
  // - chat.message：讨论区消息（全房间，AI 看不见这条通道）。
  // - check.request/san.check.request：两段式玩家掷骰（feat/keeper-agent）——
  //   守秘人裁决需要检定后推的"待掷"通知。是自己的检定就出现掷骰卡片，
  //   否则只提示"等待谁来掷"。
  // - check.result/san.check.result：服务端权威生成的骰值，渲染成一条掷骰
  //   消息；命中当前待掷卡片的 id 就把卡片收起。san.check.result 顺带把
  //   sanRemaining 写回角色卡 store（真人实测 09-#4：此前只渲染消息，从不
  //   更新角色卡，San 值永远是建卡快照）。
  // - character.stat_changed：HP 变更的结构化广播（同上 09-#4），渲染成
  //   独立的系统提示（不再混进守秘人的叙事气泡，09-#6），全房间可见；只有
  //   目标是自己时才更新本地角色卡（targetId === playerId）。
  // - error：ACTION_IN_PROGRESS（有人正在等守秘人回应）/CHECK_NOT_PENDING
  //   （待掷检定已失效）等，转成友好的系统提示。
  useEffect(() => {
    // 按 playerId 找显示名——自己用角色名/昵称，其他人查房间成员列表。
    // 🔴 一律读 ref，不读闭包捕获的值：这两个值都会在对局中途变化，进依赖
    // 就等于定期退订重订阅，窗口期的消息会丢（见上面 characterRef 处的说明）。
    const nicknameFor = (id: string) =>
      id === playerId
        ? senderNameRef.current
        : roomInfoRef.current?.players.find(p => p.playerId === id)?.nickname ?? '玩家'

    const off = onWsMessage((envelope) => {
      const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      if (envelope.type === 'action.broadcast') {
        const isSelf = envelope.payload.playerId === playerId
        // 🔴 按**事件 id** 去重，不按正文（exec/19 #42）：replay 补历史与实时
        // 广播是两条路径，同一行事件两边各来一次，得靠身份对齐。旧版拿原话
        // 当身份，于是"过个侦查"说第二次就被永久吞掉，玩家看不见自己的话。
        // 没有 eventId 时**不去重**：重复显示只是难看，丢消息是永久性的。
        if (!dedupe(`act:${envelope.payload.eventId}`, envelope.payload.eventId)) return
        setMessages(prev => [...prev, {
          type: 'player',
          sender: isSelf ? senderNameRef.current : envelope.payload.nickname,
          content: envelope.payload.utterance,
          time: now,
          isSelf,
        }])
      } else if (envelope.type === 'narration.push') {
        setTyping(false)
        if (!dedupe(`narr:${envelope.payload.eventId}`, envelope.payload.eventId)) return
        setMessages(prev => [...prev, {
          type: 'narr', sender: '守秘人', content: envelope.payload.text, time: now,
          private: envelope.payload.private === true,
        }])
      } else if (envelope.type === 'chat.message') {
        setChatMessages(prev =>
          // 按 messageId 去重：断线重连后重发（相同 clientMessageId）会拿到
          // 与第一次相同 messageId 的广播，直接丢弃即可。
          prev.some(m => m.messageId === envelope.payload.messageId)
            ? prev
            : [...prev, envelope.payload]
        )
      } else if (envelope.type === 'check.request') {
        // 球回到玩家手上了（该谁掷骰），守秘人这一拍写完了
        setTyping(false)
        const isSelf = envelope.payload.playerId === playerId
        if (isSelf) {
          setPendingCheck({ id: envelope.payload.checkRequestId, kind: 'skill', skill: envelope.payload.skill, rolling: false })
        }
        setMessages(prev => [...prev, {
          type: 'system',
          content: isSelf
            ? `守秘人请求你进行${envelope.payload.skill}检定`
            : `等待 ${nicknameFor(envelope.payload.playerId)} 进行${envelope.payload.skill}检定`,
          time: now,
        }])
      } else if (envelope.type === 'san.check.request') {
        setTyping(false)
        const isSelf = envelope.payload.playerId === playerId
        if (isSelf) {
          setPendingCheck({ id: envelope.payload.checkRequestId, kind: 'san', skill: null, rolling: false })
        }
        setMessages(prev => [...prev, {
          type: 'system',
          content: isSelf ? '守秘人请求你进行理智检定' : `等待 ${nicknameFor(envelope.payload.playerId)} 进行理智检定`,
          time: now,
        }])
      } else if (envelope.type === 'check.result') {
        const { playerId: rollerId, skill, rollValue, targetValue, result, checkRequestId } = envelope.payload
        // 对抗检定（exec/19 #38）：对手侧也由服务端掷骰，一起显示——玩家要
        // 看得见自己输在哪一掷，不能只给一个"你失败了"。
        const opposed = envelope.payload.opposedOpponent
          ? ` vs ${envelope.payload.opposedOpponent} ${envelope.payload.opposedRollValue}/${envelope.payload.opposedTargetValue} · ${envelope.payload.opposedWon ? '胜' : '负'}`
          : ''
        setMessages(prev => [...prev, {
          type: 'dice',
          sender: nicknameFor(rollerId),
          content: `${skill} · ${rollValue}/${targetValue ?? '?'} · ${result}${opposed}`,
          time: now,
          isSelf: rollerId === playerId,
        }])
        // 🔴 这里**不能**关打字指示器（exec/23 #58）。骰值与结算叙事拆成两拍
        // 广播之后（#54），中间隔着一次 10 秒级的 LLM 往返——在这里关掉的话，
        // 那十秒里指示器是灭的、输入框看起来完全空闲，玩家自然会去打字。
        // 改由「叙事到达」或「新的 check.request 到达」（球回到玩家手上）来关。
        if (checkRequestId) {
          setPendingCheck(prev => (prev && prev.id === checkRequestId ? null : prev))
        }
      } else if (envelope.type === 'san.check.result') {
        const { playerId: rollerId, rollValue, sanLoss, result, checkRequestId, sanRemaining } = envelope.payload
        setMessages(prev => [...prev, {
          type: 'dice',
          sender: nicknameFor(rollerId),
          content: `理智 · ${rollValue} · ${result} · San -${sanLoss}`,
          time: now,
          isSelf: rollerId === playerId,
        }])
        // 同上：结算叙事还没到，指示器不能在这里关（exec/23 #58）
        if (checkRequestId) {
          setPendingCheck(prev => (prev && prev.id === checkRequestId ? null : prev))
        }
        // 角色卡的 San 此前一直是建卡快照，从不随检定结果更新（真人实测
        // 09-#4）——sanRemaining 后端早就带了，只是没人读。
        // 不本地增量改，重拉一次——服务端才是权威，掉线期间错过的广播这样
        // 也能一并归位（sanRemaining 仍然带在广播里，用来渲染下面的提示）。
        if (rollerId === playerId && typeof sanRemaining === 'number') {
          reloadCharacterRef.current()
        }
      } else if (envelope.type === 'character.stat_changed') {
        // HP 变更的结构化广播（真人实测 09-#4/#6）：此前 HP 变化只被拼进
        // 叙事正文当纯文本、混在守秘人的话里（09-#6 指出这不该是它说的话）。
        // 现在渲染成独立的系统提示（跟 check.request 的"守秘人请求你进行
        // XX检定"同一类气泡），只更新**自己**的角色卡，但提示对全房间可见——
        // HP 变化在 COC 里本来就是桌面上大家都看得见的公开信息。
        const { playerId: targetId, hp, reason } = envelope.payload
        const isSelf = targetId === playerId
        const prevHp = isSelf ? characterRef.current?.derived.hp : undefined
        if (isSelf) {
          reloadCharacterRef.current()
        }
        const label = prevHp !== undefined && prevHp !== hp
          ? `${nicknameFor(targetId)} · HP ${prevHp} → ${hp}`
          : `${nicknameFor(targetId)} · HP → ${hp}`
        setMessages(prev => [...prev, {
          type: 'system',
          content: reason ? `🎲 ${label}（${reason}）` : `🎲 ${label}`,
          time: now,
        }])
      } else if (envelope.type === 'error') {
        // QUEUED 不是错误，是**回执**：话已经记下了，守秘人处理完手头这轮就会
        // 回到你（exec/19 #36）。所以这一支不清打字指示——它确实还在写。
        const queued = envelope.payload.code === 'QUEUED'
        if (!queued) setTyping(false)
        const friendly =
          queued
            ? '守秘人正在回应其他人，你的话已记下'
            : envelope.payload.code === 'ACTION_IN_PROGRESS'
            ? '守秘人正在处理其他玩家的行动，请稍候再试'
            : envelope.payload.code === 'INTERNAL_ERROR'
              ? '守秘人暂时无法回应，请稍后重试'
              : envelope.payload.code === 'CHECK_NOT_PENDING'
                ? '这个检定已经失效，等待守秘人的最新指示吧'
                : null
        if (envelope.payload.code === 'CHECK_NOT_PENDING') {
          setPendingCheck(null)
        }
        if (friendly) {
          setMessages(prev => [...prev, { type: 'system', content: friendly, time: now }])
        }
      }
    })
    return off
  }, [playerId, roomId])

  // 讨论区历史：进房拉一次（倒序返回，反转成时间正序渲染）。实时增量走上面
  // 的 chat.message 广播，历史和增量之间的重复靠 messageId 去重兜住。
  useEffect(() => {
    if (!roomId || !reconnectToken) return
    sdk.rooms
      .listMessages(roomId, reconnectToken)
      .then((history) => {
        const ordered = [...history].reverse()
        setChatMessages(prev => {
          const known = new Set(prev.map(m => m.messageId))
          return [...ordered.filter(m => !known.has(m.messageId)), ...prev]
        })
      })
      .catch(() => {}) // 历史拉不到不阻塞进房，聊天区从空开始
  }, [roomId, reconnectToken])

  // 队友角色卡：只在成员面板打开时拉一次。每次打开都重拉——队友可能刚建完卡、
  // HP 也可能刚被扣过，缓存住会展示过期数值。
  useEffect(() => {
    if (openPanel !== 'members' || !roomId || !reconnectToken) return
    let cancelled = false
    sdk.rooms
      .listPartyCharacters(roomId, reconnectToken)
      .then((list) => {
        if (!cancelled) setPartyCharacters(list)
      })
      .catch(() => {
        if (!cancelled) setPartyCharacters([])
      })
    return () => {
      cancelled = true
    }
  }, [openPanel, roomId, reconnectToken])

  // 语音输入（issue #107）：转写文本追加进输入框，用户确认后照常点发送——
  // 发送路径与手动打字完全一致，转写不直接发送（给用户一次改错的机会）。
  const speech = useSpeechInput((text) => setInput(prev => (prev ? prev + text : text)))

  // 🔴 只在**自己这一轮还在跑**时锁输入，不是"守秘人一忙就全场禁言"
  // （exec/23 #58）。`typing` 只由自己的提交/掷骰置位，所以多人局里别人回合
  // 期间你照样能说话——那是 exec/19 #36 定过的规矩：真人桌上不存在"你这句
  // 无效、请重说"，你说出口的话在空气里，服务端会缓冲进下一轮。
  //
  // 手头还有自己的待掷检定时也锁：此刻该做的是掷骰，不是打字（打了也会被
  // 守秘人的 pending 守卫挡回来）。讨论区永远不锁——那是玩家之间的通道。
  const myPendingRoll = pendingCheck !== null
  const dmBusy = channel === 'dm' && (typing || myPendingRoll)

  const sendMessage = (e?: FormEvent) => {
    e?.preventDefault()
    const text = input.trim()
    if (!text || !playerId) return
    // 回车提交绕得过按钮的 disabled，这里再拦一次
    if (channel === 'dm' && dmBusy) return
    if (channel === 'chat') {
      // 讨论区：发送前生成稳定 ID，发送失败时恢复输入框内容——
      // SDK 在 WS 非 OPEN 时静默丢弃，用户无感知；保留输入让用户重试，
      // 同时复用同一个 clientMessageId 保证幂等（issue #107 review 修复）。
      const clientMessageId = crypto.randomUUID()
      const pendingText = text
      setInput('')
      try {
        sdk.roomSocket.sendChat(playerId, { text: pendingText, clientMessageId })
      } catch {
        setInput(pendingText)
      }
      return
    }
    setInput('')
    setTyping(true)
    sdk.roomSocket.submitAction(playerId, {
      utterance: text,
      ...(privateAction ? { visibility: 'private' as const } : {}),
    })
    setPrivateAction(false)
  }

  // 掷骰确认（两段式玩家掷骰）：骰值由服务端权威生成，这里只发 checkRequestId
  // 表明"确认掷这一个"。置 rolling 防连点；结果广播回来后卡片会被收起
  // （见上面 check.result/san.check.result 的处理），rolling 状态随卡片一起消失。
  const handleRollCheck = () => {
    if (!pendingCheck || !playerId || pendingCheck.rolling) return
    setPendingCheck(prev => (prev ? { ...prev, rolling: true } : prev))
    setTyping(true)
    if (pendingCheck.kind === 'san') {
      sdk.roomSocket.rollSanCheck(playerId, { checkRequestId: pendingCheck.id })
    } else {
      sdk.roomSocket.rollCheck(playerId, { checkRequestId: pendingCheck.id })
    }
  }

  const handleDiceResult = (result: number, diceType: DiceType) => {
    const typeLabel = diceType.toUpperCase()
    const resultLabel = diceType === 'd100' ? (result <= 5 ? '极限成功' : result <= 65 ? '成功' : '失败') : `掷出 ${result}`
    setMessages(prev => [...prev, {
      type: 'dice', sender: senderName, content: `${typeLabel} · ${result}`, time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }), isSelf: true,
    }, {
      type: 'narr', sender: '守秘人', content: `检定结果: ${resultLabel}`, time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
    }])
  }

  // 结束游戏——仅房主可操作。房间转「已完成」后只能在「我的游戏」里查看复盘，不能再回到聊天室。
  const handleEndGame = async () => {
    if (!roomId) return
    setEnding(true)
    setEndError('')
    try {
      await endGame(roomId)
      disconnectWebSocket()
      navigate('/home')
    } catch (err) {
      setEndError(friendlyErrorMessage(err, '结束游戏失败'))
      setEnding(false)
    }
  }

  // 退出（不是结束游戏）——只是自己离开，房间对其他人继续存在、phase 不变，
  // 之后可以从「我的游戏」用同一个身份重新进来（见 MyRoomsPage 的继续逻辑）。
  const handleExit = () => {
    disconnectWebSocket()
    navigate('/home')
  }

  return (
    // 🔴 `theme-coc`：卷宗主题的作用域边界。语义 token 在这里换成暗色，
    // 作用域外（大厅/建房/建卡）继续用平台中性色——UI 改造是一页一页做的，
    // 全站同时切会留下一堆"暗但没做过"的半成品页。
    // `desk-grain`/`desk-lamp` 把这一层变成一张被台灯照着的胡桃木桌面。
    <div className="theme-coc desk-grain desk-lamp desk-sigil h-full flex flex-col bg-card relative max-w-[430px] mx-auto overflow-hidden">
      {/* Header */}
      <div className="relative z-[1] flex items-center gap-2.5 px-3 py-2 border-b border-black/60 bg-page flex-shrink-0">
        <button
          onClick={() => setConfirmExit(true)}
          aria-label="退出"
          className="cut-corner w-[30px] h-[30px] bg-black/30 border border-brass/35 flex items-center justify-center active:bg-brass/20"
        >
          <ArrowLeft className="w-[15px] h-[15px] text-brass-bright" strokeWidth={2.5} />
        </button>
        <div className="w-[30px] h-[30px] bg-black/25 border border-black/50 flex items-center justify-center text-[15px] flex-shrink-0">
          🏚️
        </div>
        <div className="flex-1 min-w-0">
          <div className="font-display text-sm text-text-primary tracking-[0.06em] truncate">
            {roomInfo?.moduleTitle || '对局中'}
          </div>
          <div className="typed text-[9.5px] text-text-muted mt-px">
            {roomInfo ? `${roomInfo.players.length} 位调查员` : '克苏鲁的呼唤'}
          </div>
        </div>
        {/* 顶栏原本还有一个开「队友角色卡」的小人图标，与底部那个带文字的
            「队友」tab 开的是同一个面板（`openPanel === 'members'`）。删掉图标
            保留 tab：exec/14 P5.3 做完队友卡之后入口只有这个无文字图标，真人
            实测的反馈是"没有实现"——功能完整但找不到，底部 tab 正是为此加的。
            留图标等于把那次修复退回去。顶部的「N 位调查员」已经在表达同一件事。 */}
      </div>

      {/* 退出确认——不是结束游戏，房间对其他人继续存在 */}
      {confirmExit && (
        <div className="fixed inset-0 z-40 bg-black/62 flex items-center justify-center px-7" onClick={() => setConfirmExit(false)}>
          {/* 一页通知：走书页材质，不是圆角对话框 */}
          <div
            className="leaf paper-grain bg-book text-ink px-[18px] pt-5 pb-4 w-full max-w-[300px] -rotate-[0.7deg]"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="typed text-[9px] text-ink/50 border-b border-ink/20 pb-1.5 mb-3">离场</div>
            <p className="font-display text-sm leading-[1.85] mb-3.5">
              确定要退出游戏吗？<br />房间会保留，之后可以从「我的游戏」继续。
            </p>
            <div className="flex gap-2">
              <button onClick={() => setConfirmExit(false)}
                className="typed flex-1 py-2 text-[10px] bg-transparent border border-ink/30 text-ink-soft active:bg-ink/10">
                取消
              </button>
              <button onClick={handleExit}
                className="typed flex-1 py-2 text-[10px] bg-rust text-book border-none active:bg-rust-dark">
                确认退出
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 频道切换（issue #107）：主持人 = 跟 AI 的对话（全房间可见、进 AI 上下文）；
          讨论区 = 玩家之间商量（AI 完全看不见）。两个独立界面共用下方输入框，
          发送按当前频道分流。 */}
      <div className="relative z-[1] flex bg-panel border-b border-black/50 flex-shrink-0">
        {([
          { key: 'dm', label: '主持人', icon: Scroll },
          { key: 'chat', label: '讨论区', icon: MessagesSquare },
        ] as const).map((tab) => (
          <button
            key={tab.key}
            onClick={() => setChannel(tab.key)}
            className={`flex-1 py-2.5 text-[12px] font-semibold flex items-center justify-center gap-1.5 transition-colors border-b-2 ${
              channel === tab.key
                ? 'text-brass-bright border-brass'
                : 'text-text-muted border-transparent'
            }`}
          >
            <tab.icon className="w-3.5 h-3.5" strokeWidth={2} />
            {tab.label}
          </button>
        ))}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-4 flex flex-col gap-3" id="chatScroll">
        {channel === 'chat' ? (
          chatMessages.length === 0 ? (
            <div className="text-center py-8 text-text-muted text-sm">
              这里是玩家讨论区，商量对策吧——守秘人听不到这里的话
            </div>
          ) : (
            chatMessages.map((msg) => {
              const isSelf = msg.playerId === playerId
              return (
                <div key={msg.messageId} className={`flex gap-2.5 ${isSelf ? 'flex-row-reverse' : ''} animate-[msgIn_0.3s_ease]`}>
                  <div className={`w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center text-sm border border-border-light ${isSelf ? 'bg-[#eef6ee]' : 'bg-panel'}`}>
                    💬
                  </div>
                  <div className={`flex-1 min-w-0 ${isSelf ? 'text-right' : ''}`}>
                    <div className={`text-[11px] font-semibold mb-0.5 ${isSelf ? 'text-mold' : 'text-text-muted'}`}>
                      {msg.nickname}
                    </div>
                    <div className={`text-sm leading-[1.65] text-text-body inline-block max-w-full px-3.5 py-2.5 rounded-md text-left ${isSelf ? 'bg-[#eef6ee]' : 'bg-panel'}`}>
                      {msg.text}
                    </div>
                    <div className="text-[10px] text-text-dim mt-0.5">
                      {new Date(msg.sentAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
                    </div>
                  </div>
                </div>
              )
            })
          )
        ) : messages.map((msg, i) => {
          // 系统提示：居中的一枚小铭牌，不是聊天软件的圆胶囊
          if (msg.type === 'system') {
            return (
              // 🔴 系统提示改成**一枚浅色小纸签**。原来是深底深字加细边，
              // 在木桌上几乎读不出来——机制信息不该比故事更难看清。
              // 材质上也说得通：它是别在案卷上的一张便条。
              <div key={i} className="flex justify-center py-0.5 animate-[fadeIn_0.3s_ease]">
                <span className="typed text-[10px] text-ink bg-book/90 border border-ink/25 px-3 py-1 shadow-[0_1px_3px_rgba(0,0,0,.4)]">
                  {msg.content}
                </span>
              </div>
            )
          }

          if (msg.type === 'dice') {
            return (
              <div key={i} className="flex flex-row-reverse gap-2 items-start animate-[msgIn_0.3s_ease]">
                <div className="w-[26px] h-[26px] flex-shrink-0 flex items-center justify-center text-[13px] bg-mold/15 border border-mold/50">
                  🎲
                </div>
                <div className="min-w-0 text-right">
                  <div className="typed text-[9px] text-mold mb-[3px]">{msg.sender} · 掷骰</div>
                  <div className="font-mono text-[13px] text-text-body bg-mold/12 border-l-2 border-mold px-2.5 py-1.5 inline-block text-left">
                    {msg.content}
                  </div>
                  <div className="typed text-[9px] text-text-dim mt-1">{msg.time}</div>
                </div>
              </div>
            )
          }

          const isNarr = msg.type === 'narr'
          // 🔴 空间规则：左 = 主持人，右 = 玩家们（自己与队友都靠右）。
          // 讨论区没有主持人，左边空出来了 → 队友回到左边，左右分栏更好读。
          const isSelf = msg.type === 'player' && msg.isSelf
          const onRight = channel === 'dm' ? !isNarr : isSelf

          // 守秘人 = 书页：裁齐的边、版心、装订侧阴影、页码
          if (isNarr) {
            return (
              // 🔴 书页是**满幅铺开**的，不是浮在桌面上的一张卡片。
              // 真机反馈「太条条框框」：四条边都框着 + 页眉一道横线 + 投影，
              // 读起来是"卡片"不是"书页"。改法是让它**出血到两侧边缘**
              // （-mx 抵消消息区的内边距）、去掉投影与页眉横线，
              // 署名/时间缩成右上角一行浅字。框由界面本身提供，纸不自带框。
              // 🔴 回到**消息形态**：头像 + 名字 + 气泡 + 时间，左对齐。
              // 上一版做成整页书（首字下沉、段末花饰、页脚落款）被判"很怪"——
              // 那是排一本书的规矩，不是"有人在跟你说话"。守秘人是个**在场的人**，
              // 他说话就该长得像说话。材质保留（纸色 + 纸纹 + 撕边），
              // 但布局回到消息的语法。
              <div key={i} className="flex gap-2 items-start animate-[msgIn_0.3s_ease]">
                <div className="w-[30px] h-[30px] mt-0.5 flex-shrink-0 flex items-center justify-center text-[15px] bg-brass/15 border border-brass/45">
                  📜
                </div>
                <div className="min-w-0 flex-1">
                  <div className="typed flex items-baseline gap-2 mb-1">
                    <span className="text-[10px] text-brass-bright">{msg.sender}</span>
                    <span className="text-[8.5px] text-text-dim">{msg.time}</span>
                  </div>
                  <div className="kp-bubble paper-grain relative bg-book text-ink px-3 py-2.5">
                    <p className="font-display text-[13.5px] leading-[1.78] whitespace-pre-wrap">{msg.content}</p>
                  </div>
                </div>
              </div>
            )
          }

          // 玩家 = 便签 + 回形针。自己与队友三处不同：纸色、针的金属、名字后缀
          return (
            <div
              key={i}
              className={`max-w-[82%] flex flex-col gap-[3px] animate-[msgIn_0.3s_ease] ${
                onRight ? 'self-end items-end' : 'self-start items-start'
              }`}
            >
              <div
                className={`memo paper-grain relative w-full text-ink pl-[30px] pr-3 pt-3 pb-2.5 mt-2.5 ${
                  isSelf ? 'bg-memo-self rotate-[0.8deg]' : 'bg-memo-mate -rotate-[0.7deg]'
                }`}
              >
                <Paperclip metal={isSelf ? 'brass' : 'steel'} />
                <div className={`typed text-[9px] text-ink/55 mb-1.5 ${onRight ? 'text-right' : 'text-left'}`}>
                  {msg.sender}
                  {isSelf && <span className="text-rust"> · 你</span>}
                </div>
                {msg.private && !revealedPrivate.has(i) ? (
                  <button
                    type="button"
                    onClick={() => setRevealedPrivate(prev => new Set(prev).add(i))}
                    className={`flex items-center gap-1.5 text-[12px] text-ink/60 w-full ${onRight ? 'justify-end' : ''}`}
                  >
                    <EyeOff className="w-3.5 h-3.5" strokeWidth={2} />
                    只有你能看到 · 点按查看
                  </button>
                ) : (
                  <p className={`font-display text-[13.5px] leading-[21px] whitespace-pre-wrap ${onRight ? 'text-right' : 'text-left'}`}>
                    {msg.content}
                  </p>
                )}
              </div>
              <span className="typed text-[9px] text-text-muted">{msg.time}</span>
            </div>
          )
        })}

        {/* Typing indicator（只属于主持人频道——讨论区没有"守秘人正在输入"这回事） */}
        {channel === 'dm' && typing && (
          <div className="flex gap-2.5 animate-[msgIn_0.3s_ease]">
            <div className="w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center text-sm bg-[#faf5eb] border border-brass">
              📜
            </div>
            <div className="bg-panel inline-flex gap-1 items-center px-4 py-3 rounded-md">
              {[0, 1, 2].map((i) => (
                <span key={i} className="w-1.5 h-1.5 bg-brass rounded-full animate-bounce"
                  style={{ animationDelay: `${i * 0.2}s`, animationDuration: '1.4s' }} />
              ))}
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Action Bar */}
      <div className="relative z-[1] flex bg-panel border-t border-black/55 flex-shrink-0">
        {[
          { icon: ScrollText, label: '角色卡', key: 'sheet' },
          { icon: Star, label: '技能', key: 'skills' },
          // 队友卡此前只能从顶栏那个无文字的小人图标进（面板还叫「房间成员」，
          // 读起来像"看看谁在线"）——真人实测直接找不到，以为功能没做。
          // 底部这排带文字的 tab 才是玩家找"卡"的心智位置，跟自己的角色卡并列。
          { icon: Users, label: '队友', key: 'members' },
          { icon: Map, label: '地图', key: 'map' },
          { icon: BookOpen, label: '速记', key: 'notes' },
        ].map((item) => (
          <button
            key={item.key}
            onClick={() => setOpenPanel(openPanel === item.key ? null : item.key)}
            className={`flex-1 py-1.5 px-1 bg-none border-none text-[10px] font-medium cursor-pointer flex flex-col items-center gap-[3px] font-sans transition-colors ${
              openPanel === item.key ? 'text-brass-bright bg-brass/15' : 'text-text-muted'
            }`}
          >
            <item.icon className="w-5 h-5" strokeWidth={1.5} />
            {item.label}
          </button>
        ))}
      </div>

      {/* HP/SAN 实时状态条——放在角色卡/技能等快捷面板和输入框之间，聊天时想随时
          瞄一眼当前状态不用点开面板。HP 目前没有"当前值/上限值"两套数字（还没
          做受伤扣血的机制，见已知局限），先按"当前即满值"画满条，以后接了扣血
          机制这里会自然跟着变化。 */}
      {character && (
        // 方头刻度条，不是圆角进度条——机制数值该像量表
        <div className="relative z-[1] flex items-center gap-3.5 px-3.5 py-1.5 border-t border-black/50 bg-page flex-shrink-0">
          <div className="flex items-center gap-1.5 flex-1 min-w-0">
            <Heart className="w-[11px] h-[11px] text-mold flex-shrink-0" strokeWidth={2.4} />
            <span className="typed text-[9px] text-text-muted flex-shrink-0">HP</span>
            <div className="flex-1 h-[5px] bg-black/50 overflow-hidden">
              <div className="h-full bg-mold" style={{ width: '100%' }} />
            </div>
            <span className="font-mono text-[11px] text-mold flex-shrink-0 tabular-nums">{character.derived.hp}</span>
          </div>
          <div className="flex items-center gap-1.5 flex-1 min-w-0">
            <span className="typed text-[9px] text-text-muted flex-shrink-0">SAN</span>
            <div className="flex-1 h-[5px] bg-black/50 overflow-hidden">
              <div className="h-full bg-[#8a72ad]" style={{ width: `${Math.min(100, character.derived.san)}%` }} />
            </div>
            <span className="font-mono text-[11px] text-[#a795c4] flex-shrink-0 tabular-nums">{character.derived.san}</span>
          </div>
        </div>
      )}

      {/* 待掷检定卡片（两段式玩家掷骰，feat/keeper-agent）：守秘人裁决需要
          检定后推给本人，骰值由服务端权威生成——点击才真正掷骰。 */}
      {channel === 'dm' && pendingCheck && (
        <div className="relative z-[1] px-3 pt-2 flex-shrink-0 space-y-1.5">
          {showDiceTip && (
            <div className="paper-grain relative text-[11.5px] leading-[1.65] text-ink bg-book border-l-[3px] border-brass px-3 py-2.5">
              <strong className="font-semibold">两段式掷骰：</strong>
              守秘人已发起检定，请点右侧「掷骰」由服务器生成结果；
              不点则剧情会停在这里（不是卡死）。下方自由骰与本次检定无关。
              <button
                type="button"
                className="typed ml-1 text-[10px] underline text-rust"
                onClick={() => {
                  setShowDiceTip(false)
                  try {
                    sessionStorage.setItem('aidm-dice-tip-seen', '1')
                  } catch { /* ignore */ }
                }}
              >
                知道了
              </button>
            </div>
          )}
          {/* 待掷 = 一张盖了章的表单，横排：文案在左、掷骰在右。
              ⚠️ 这里必须用 `{'{'}/* *{'/'}{'}'}` 而不是 `//`——JSX 的 children 位置里
              `//` 不是注释，是**会被渲染出来的文本**。 */}
          <div className="paper-grain relative flex items-center gap-2.5 bg-dossier text-ink border-l-4 border-double border-rust px-3 py-2.5 shadow-[0_2px_0_rgba(0,0,0,.4),0_8px_16px_rgba(0,0,0,.42)]">
            <span className="flex-1 text-[12.5px] font-semibold">
              守秘人请求：{pendingCheck.skill ? `${pendingCheck.skill}检定` : '理智检定'}
              {!pendingCheck.rolling && <em className="typed not-italic block text-[10px] text-ink-soft mt-0.5">点击掷骰</em>}
            </span>
            <button
              onClick={() => {
                if (showDiceTip) {
                  setShowDiceTip(false)
                  try {
                    sessionStorage.setItem('aidm-dice-tip-seen', '1')
                  } catch { /* ignore */ }
                }
                handleRollCheck()
              }}
              disabled={pendingCheck.rolling}
              className="typed px-3.5 py-2 bg-ink text-book text-[10px] flex-shrink-0 active:bg-rust disabled:opacity-50 transition-colors"
            >
              {pendingCheck.rolling ? '掷骰中…' : '掷骰'}
            </button>
          </div>
        </div>
      )}

      {/* Input area：同一个输入框按当前频道分流（主持人 → action.submit，
          讨论区 → chat.send）。麦克风是语音输入（issue #107）：浏览器本地转写
          成文字填进输入框，之后跟手动打字完全一样；浏览器不支持时按钮不渲染。 */}
      <div className="relative z-[1] border-t border-black/55 bg-page px-3 py-2.5 flex-shrink-0">
        <form onSubmit={sendMessage} className="flex gap-[7px] items-center">
          {speech.supported && (
            <button
              type="button"
              onClick={() => (speech.listening ? speech.stop() : speech.start())}
              className={`cut-corner w-[38px] h-[38px] border flex items-center justify-center flex-shrink-0 active:scale-[0.94] transition-all ${
                speech.listening
                  ? 'bg-mold border-mold text-page animate-pulse'
                  : 'bg-black/30 border-black/55 text-text-muted active:border-brass active:text-brass-bright'
              }`}
            >
              <Mic className="w-[18px] h-[18px]" strokeWidth={2} />
            </button>
          )}
          {channel === 'dm' && (
            <button
              type="button"
              onClick={() => setPrivateAction(v => !v)}
              title={privateAction ? '这一条只有你自己看得到' : '设为私密行动'}
              className={`cut-corner w-[38px] h-[38px] border flex items-center justify-center flex-shrink-0 active:scale-[0.94] transition-all ${
                privateAction
                  ? 'bg-brass border-brass text-page'
                  : 'bg-black/30 border-black/55 text-text-muted active:border-brass active:text-brass-bright'
              }`}
            >
              <EyeOff className="w-[18px] h-[18px]" strokeWidth={2} />
            </button>
          )}
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            readOnly={dmBusy}
            placeholder={
              channel === 'chat'
                ? '和队友讨论…'
                : dmBusy
                  ? (myPendingRoll ? '先掷骰吧…' : '守秘人正在回应…')
                  : privateAction
                    ? '私密行动，只有你看得到…'
                    : '对守秘人说…'
            }
            // 🔴 输入框是**一张纸**，不是深色槽。
            // 真机上"深槽压深框"两者只差几个色阶，字打上去根本看不见；而且材质
            // 也不对——你打的这段话下一秒就会变成一张便签，那就该在纸上写。
            // 浅纸 + 深墨在深色框里天然最显眼，不用靠加边框描亮。
            className={`memo-input flex-1 min-w-0 bg-memo-self text-ink border-none px-3 py-2 font-display text-[15px] outline-none h-[38px] placeholder:text-ink/40 transition-shadow ${
              dmBusy ? 'opacity-70' : ''
            }`}
          />
          <button
            type="submit"
            disabled={dmBusy}
            aria-label="送出"
            className="cut-corner h-[38px] px-3.5 bg-brass border-none text-page flex items-center justify-center flex-shrink-0 active:scale-[0.96] transition-all hover:bg-brass-dark disabled:opacity-40 disabled:active:scale-100"
          >
            <SendHorizontal className="w-4 h-4" strokeWidth={2.5} />
          </button>
        </form>
      </div>

      {/* ── Panels ── */}

      {/* Panel: 角色卡（真实建卡数据，不再是写死的示例角色）。分两页——技能已经有
          单独的底部按钮，这里不重复放。 */}
      <BottomPanel accent="#8a6a2e" paper="#cbb894" open={openPanel === 'sheet'} onClose={() => setOpenPanel(null)} title={`调查员 · ${character?.info.name || '未建卡'}`}>
        {character ? (
          <>
            <div className="flex gap-1.5 mb-3.5">
              {[{ key: 'info', label: '基本信息' }, { key: 'background', label: '背景装备' }].map((p) => (
                <button key={p.key} onClick={() => setSheetPage(p.key as typeof sheetPage)}
                  className={`cut-corner typed flex-1 text-center text-[10px] py-2 border transition-all ${
                    sheetPage === p.key ? 'bg-ink text-dossier border-ink' : 'bg-transparent text-text-muted border-text-muted/40'
                  }`}>
                  {p.label}
                </button>
              ))}
            </div>

            {sheetPage === 'info' && (
              <>
                <div className="flex items-center gap-3 mb-3.5">
                  <div className="w-12 h-14 rounded-sm flex items-center justify-center text-2xl"
                    style={{ background: 'linear-gradient(135deg,#e8e0d0,#d8cfb8)', border: '2px solid #b8976a' }}>
                    🕵️
                  </div>
                  <div>
                    <div className="text-sm font-semibold text-text-primary">{character.info.name}</div>
                    <div className="text-[11px] text-text-muted">
                      {character.info.age}岁 · {character.info.gender} · {character.info.occupationId ? ruleset?.occupations.find(o => o.id === character.info.occupationId)?.name : '未选择职业'}
                    </div>
                  </div>
                </div>

                <Section label="籍贯" tone="form">
                  <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
                    <div className="flex items-center justify-between border-b border-dotted border-ink/30 pb-1">
                      <span className="typed text-[9px] text-ink/55">居住地</span>
                      <span className="font-display text-[13px] text-ink">{character.info.residence || '—'}</span>
                    </div>
                    <div className="flex items-center justify-between border-b border-dotted border-ink/30 pb-1">
                      <span className="typed text-[9px] text-ink/55">出生地</span>
                      <span className="font-display text-[13px] text-ink">{character.info.birthplace || '—'}</span>
                    </div>
                  </div>
                </Section>

                {/* 衍生值是**机制读数**，所以做成仪表盘：等宽数字 + 刻度感的方格，
                    跟上面的填空栏、下面的属性表都不一样 */}
                <div className="mt-[18px] flex gap-1.5">
                  {[
                    { label: 'HP', value: `${character.derived.hp}`, color: 'text-mold' },
                    { label: 'SAN', value: `${character.derived.san}`, color: 'text-[#7050a0]' },
                    { label: 'MP', value: `${character.derived.mp}`, color: 'text-[#4a7098]' },
                    { label: 'DB', value: character.derived.db, color: 'text-text-muted' },
                    { label: 'MOV', value: `${character.derived.move}`, color: 'text-text-muted' },
                  ].map((pill) => (
                    <div
                      key={pill.label}
                      className="flex-1 border-t-2 border-ink/50 border-x border-b border-x-ink/15 border-b-ink/15 bg-black/[0.06] px-1 py-1.5 text-center"
                    >
                      <div className="typed text-[8.5px] text-ink/55">{pill.label}</div>
                      <div className={`text-[17px] font-bold font-mono tabular-nums ${pill.color}`}>{pill.value}</div>
                    </div>
                  ))}
                </div>

                <Section label="基础属性" tone="form">
                  <div className="grid grid-cols-2 gap-x-3 gap-y-1">
                  {/* 属性清单由后端 ruleset 驱动，前端不再自己维护一份名单——
                      此前三处各硬编码一份，加幸运时漏改一处就导致角色卡看不到
                      幸运值（issue #96）。 */}
                  {(ruleset?.attributes ?? []).map(attribute => (
                    <div
                      key={attribute.key}
                      className="flex items-baseline justify-between border-b border-dotted border-ink/30 py-1"
                    >
                      <span className="typed text-[9px] text-ink/55">{attribute.key}</span>
                      <span className="font-mono text-[15px] font-bold text-ink tabular-nums">
                        {character.attr[attribute.key]}
                      </span>
                    </div>
                  ))}
                  </div>
                </Section>
              </>
            )}

            {sheetPage === 'background' && (
              <>
                {/* 🔴 三段内容性质不同，皮也不同：装备是逐条清单、背景故事是
                    打字报告、备注是手写便条。空的字段显式画成空框，不是灰句子。 */}
                <Section label="装备" tone="list">
                  {character.equipment ? (
                    <p className="text-[13px] text-ink/85 leading-[1.75] whitespace-pre-wrap">{character.equipment}</p>
                  ) : (
                    <Blank>未填写</Blank>
                  )}
                </Section>

                <Section label="背景故事" tone="prose">
                  {character.background ? (
                    <p className="font-display text-[13.5px] text-ink/90 leading-[1.85] whitespace-pre-wrap">
                      {character.background}
                    </p>
                  ) : (
                    <Blank>未填写</Blank>
                  )}
                </Section>

                <Section label="备注" tone="note">
                  {character.notes ? (
                    <p className="font-mono text-[12px] text-ink/85 leading-[22px] whitespace-pre-wrap">{character.notes}</p>
                  ) : (
                    <Blank>未填写</Blank>
                  )}
                </Section>

                {/* 结构化背景故事（character-build-migration）：建卡向导里填的
                    8 个引导字段，此前只存进了 character-store，没有任何地方
                    渲染出来——建过卡之后这些内容进游戏就再也看不到。只展示
                    玩家实际填过的字段，全空则不显示这个区块，避免一堆
                    "未填写"占屏幕。 */}
                {character.backgroundDetail &&
                  BACKGROUND_DETAIL_FIELDS.some(({ key }) => character.backgroundDetail?.[key]) && (
                    <>
                      {/* 8 个引导字段做成**逐条的档案分录**：左侧一道细线 +
                          序号，跟上面三大段拉开层级——它们是"细节"，不是并列的章。 */}
                      <div className="mt-6 mb-1 flex items-center gap-2">
                        <span className="typed text-[9px] text-ink/60">背景故事细节</span>
                        <span className="flex-1 h-px bg-ink/20" />
                      </div>
                      {BACKGROUND_DETAIL_FIELDS.filter(({ key }) => character.backgroundDetail?.[key]).map(
                        ({ key, label }, idx) => (
                          <div key={key} className="flex gap-2.5 pt-2.5">
                            <span className="typed text-[9px] text-ink/35 pt-0.5 w-4 flex-shrink-0 text-right">
                              {String(idx + 1).padStart(2, '0')}
                            </span>
                            <div className="flex-1 min-w-0 border-l border-ink/20 pl-2.5">
                              <div className="typed text-[9px] text-ink/55 mb-1">{label}</div>
                              <p className="font-display text-[13px] text-ink/85 leading-[1.8]">
                                {character.backgroundDetail?.[key]}
                              </p>
                            </div>
                          </div>
                        )
                      )}
                    </>
                  )}
              </>
            )}
          </>
        ) : (
          <p className="text-sm text-text-dim py-6 text-center">还没有创建角色</p>
        )}
      </BottomPanel>

      {/* Panel: 技能——按职业技能/兴趣技能分两页，各自按数值从高到低排列。
          固定半屏高度，两个页签内容多少不一样也不会让面板忽高忽低。 */}
      <BottomPanel accent="#4e6b3e" paper="#c5c2a4" open={openPanel === 'skills'} onClose={() => setOpenPanel(null)} title="技能" heightVh={50}>
        {character ? (
          <>
            <div className="flex gap-1.5 mb-3.5">
              {[{ key: 'occupation', label: '职业技能' }, { key: 'interest', label: '兴趣技能' }].map((t) => (
                <button key={t.key} onClick={() => setSkillsTab(t.key as typeof skillsTab)}
                  className={`cut-corner typed flex-1 text-center text-[10px] py-2 border transition-all ${
                    skillsTab === t.key ? 'bg-ink text-dossier border-ink' : 'bg-transparent text-text-muted border-text-muted/40'
                  }`}>
                  {t.label}
                </button>
              ))}
            </div>
            <div className="space-y-2">
              {(() => {
                const occSkillIds = character.info.occupationId
                  ? ruleset?.occupations.find(o => o.id === character.info.occupationId)?.skillIds ?? []
                  : []
                const list = (ruleset?.skills ?? [])
                  .filter((skill) => skillsTab === 'occupation' ? occSkillIds.includes(skill.id) : !occSkillIds.includes(skill.id))
                  .map((skill) => ({
                    skill,
                    value: character.skillFinalValues?.[skill.id] ?? 0,
                  }))
                  .sort((a, b) => b.value - a.value)
                return list.map(({ skill, value }) => (
                  <div key={skill.id} className="flex items-center gap-3 py-1.5">
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-text-primary">{skill.name}</div>
                      <div className="text-[10px] text-text-dim font-mono">{skill.nameEn}</div>
                    </div>
                    <div className="gauge flex-1 h-[7px]">
                      <div className="h-full bg-ink transition-all" style={{ width: `${value}%` }} />
                    </div>
                    <span className="text-xs font-bold font-mono text-text-muted min-w-[36px] text-right">{value}%</span>
                  </div>
                ))
              })()}
            </div>
          </>
        ) : (
          <p className="text-sm text-text-dim py-6 text-center">暂未建卡</p>
        )}
      </BottomPanel>

      {/* Panel: 地图——结构化地点未接线，不展示假数据 */}
      <BottomPanel accent="#8f3628" paper="#cfc3a2" open={openPanel === 'map'} onClose={() => setOpenPanel(null)} title="地图">
        <div className="survey flex flex-col items-center justify-center py-11 px-6 border border-ink/30 text-center">
          <Map className="w-10 h-10 text-text-dim mb-3 opacity-60" />
          <p className="text-sm text-text-primary font-medium mb-1.5">地点随叙事推进</p>
          <p className="text-xs text-text-muted leading-relaxed">
            结构化地图尚未接入。请以主持人频道的场景描写为准；
            有「当前场景」状态后这里再显示真地点。
          </p>
          {roomInfo?.moduleTitle && (
            <p className="text-[11px] text-text-dim mt-3 font-mono">{roomInfo.moduleTitle}</p>
          )}
        </div>
      </BottomPanel>

      {/* Panel: 速记 */}
      <BottomPanel accent="#3f362a" paper="#d3c49c" open={openPanel === 'notes'} onClose={() => setOpenPanel(null)} title="速记本">
        {playerIntro && (
          // 简报是**别人给你的**（打字报告），笔记是**你自己写的**（横格纸），
          // 两者性质相反，皮也相反
          <Section label="案件简报" tone="prose">
            <p className="font-display text-[13px] text-ink/90 leading-[1.85] whitespace-pre-wrap">{playerIntro}</p>
          </Section>
        )}
        <div className="flex gap-2 mb-3">
          <button onClick={() => setNotes(prev => prev + `\n\n[🔍 新线索 ${new Date().toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'})}]\n`)}
            className="cut-corner typed flex-1 py-2 bg-transparent border border-ink/30 text-ink-soft text-[10px] flex items-center justify-center gap-1 active:bg-ink/10">
            <Plus className="w-3.5 h-3.5" /> 添加线索标签
          </button>
          <button onClick={() => {
              if (!notesKey) return
              localStorage.setItem(notesKey, notes)
              setLastSaved(new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }))
            }}
            className="cut-corner typed px-4 py-2 bg-ink text-dossier text-[10px] flex items-center justify-center gap-1 active:bg-rust">
            <Save className="w-3.5 h-3.5" /> 保存
          </button>
        </div>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="📋 案件笔记"
          className="notepaper w-full min-h-[160px] text-[12px] text-ink border border-ink/28 px-2.5 py-2 resize-none outline-none focus:border-brass transition-colors font-mono placeholder:text-ink/35"
        />
        <div className="text-[10px] text-text-dim mt-2 text-right">{lastSaved ? `最后保存: ${lastSaved}` : '尚未保存'}</div>
      </BottomPanel>

      {/* Panel: 房间成员 */}
      <BottomPanel accent="#3f5f7d" paper="#c1bfb0" open={openPanel === 'members'} onClose={() => setOpenPanel(null)} title="队友角色卡">
        {roomInfo ? (
          <div className="space-y-1.5">
            <p className="text-xs text-text-muted mb-2">{roomInfo.players.length}/{roomInfo.maxPlayers} 人</p>
            {roomInfo.players.map((p) => {
              const card = partyCharacters?.find((c) => c.playerId === p.playerId)
              const expanded = expandedMemberId === p.playerId
              return (
                <div key={p.playerId} className="border border-ink/25 bg-white/15 overflow-hidden">
                  <button
                    onClick={() => setExpandedMemberId(expanded ? null : p.playerId)}
                    className="w-full flex items-center gap-3 px-3 py-2 text-left active:bg-border-light"
                  >
                    <div className="w-[26px] h-[26px] bg-ink/10 border border-ink/25 flex items-center justify-center text-[13px] flex-shrink-0">🔍</div>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-text-primary">
                        {card?.name ? `${card.name}` : p.nickname}
                      </div>
                      <div className="text-[11px] text-text-dim truncate">
                        {p.nickname}
                        {p.isHost ? ' · 房主' : ''}
                        {p.playerId === playerId ? ' · 你' : ''}
                        {card?.occupation ? ` · ${card.occupation}` : card ? '' : ' · 未建卡'}
                      </div>
                    </div>
                    <span className="text-[10px] text-text-dim flex-shrink-0">{expanded ? '收起' : '看卡'}</span>
                  </button>

                  {expanded && (
                    <div className="px-3 pb-3 border-t border-border-light pt-2.5">
                      {card && card.name ? (
                        <>
                          <div className="grid grid-cols-4 gap-1 mb-2.5">
                            {(ruleset?.attributes ?? []).map((attr) => (
                              <div key={attr.key} className="border border-ink/20 px-1.5 py-1 text-center">
                                <div className="text-[10px] text-text-dim">{attr.label}</div>
                                <div className="text-sm font-bold font-mono text-text-primary">{card.attributes?.[attr.key] ?? '—'}</div>
                              </div>
                            ))}
                          </div>
                          <div className="flex gap-1.5 mb-2.5">
                            {[
                              { label: 'HP', value: card.derivedStats?.HP },
                              { label: 'SAN', value: card.derivedStats?.SAN },
                              { label: 'MP', value: card.derivedStats?.MP },
                            ].map((s) => (
                              <div key={s.label} className="flex-1 border border-ink/20 px-2 py-1 flex items-center justify-between">
                                <span className="text-[10px] text-text-muted">{s.label}</span>
                                <span className="text-sm font-bold font-mono text-text-primary">{s.value ?? '—'}</span>
                              </div>
                            ))}
                          </div>
                          <div className="space-y-1">
                            {(ruleset?.skills ?? [])
                              .map((skill) => ({ skill, value: card.skills?.[skill.id] ?? 0 }))
                              .filter(({ value }) => value > 0)
                              .sort((a, b) => b.value - a.value)
                              .slice(0, 10)
                              .map(({ skill, value }) => (
                                <div key={skill.id} className="flex items-center gap-2">
                                  <span className="text-[11px] text-text-body flex-1 min-w-0 truncate">{skill.name}</span>
                                  <div className="gauge w-16 h-[6px]">
                                    <div className="h-full bg-ink" style={{ width: `${Math.min(value, 100)}%` }} />
                                  </div>
                                  <span className="text-[11px] font-mono text-text-muted min-w-[28px] text-right">{value}</span>
                                </div>
                              ))}
                          </div>
                        </>
                      ) : (
                        <p className="text-[11px] text-text-dim py-2 text-center">
                          {partyCharacters === null ? '正在读取角色卡…' : '这位调查员还没建卡'}
                        </p>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ) : (
          <p className="text-sm text-text-dim py-6 text-center">正在获取房间成员…</p>
        )}

        {isHost && (
          <div className="mt-4 pt-4 border-t border-border-light">
            {endError && <p className="text-[11px] text-[#c04040] text-center mb-2">{endError}</p>}
            {confirmEnd ? (
              <div className="space-y-2">
                <p className="text-xs text-text-muted text-center">确定要结束本局游戏吗？结束后将无法再回到聊天室，只能在「我的游戏」里查看复盘。</p>
                <div className="flex gap-2">
                  <button onClick={() => setConfirmEnd(false)} disabled={ending}
                    className="flex-1 py-2 rounded-sm bg-panel border border-border-light text-text-muted text-xs font-medium active:bg-border-light disabled:opacity-60">
                    取消
                  </button>
                  <button onClick={handleEndGame} disabled={ending}
                    className="flex-1 py-2 rounded-sm bg-[#c04040] text-white text-xs font-medium active:bg-[#a03030] disabled:opacity-60">
                    {ending ? '结束中…' : '确认结束'}
                  </button>
                </div>
              </div>
            ) : (
              <button onClick={() => setConfirmEnd(true)}
                className="w-full py-2 rounded-sm bg-transparent text-[#c04040] border border-[#c04040]/40 text-xs font-medium flex items-center justify-center gap-1.5 active:bg-[#c04040]/5">
                <FlagOff className="w-3.5 h-3.5" /> 结束游戏
              </button>
            )}
          </div>
        )}
      </BottomPanel>

      {/* ── Dice Modal ── */}
      <DiceModal open={showDice} onClose={() => setShowDice(false)} onResult={handleDiceResult} />
    </div>
  )
}
