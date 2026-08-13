import { useNavigate } from 'react-router-dom'
import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, BookmarkPlus, BookmarkCheck, UserPlus, Swords, Eye, RefreshCw, X } from 'lucide-react'
import { useCharacterStore } from '@/stores/character-store'
import {
  fetchCharacter,
  regenerateBackground,
  saveAsTemplate,
  deleteMyTemplate,
} from '@/services/character/character-api'
import { toCompletedCharacter } from '@/services/character/character-view'
import { BACKGROUND_DETAIL_FIELDS } from '@/data/character-model'
import { useRoomStore } from '@/stores/room-store'
import { useAuthStore } from '@/stores/auth-store'
import { connectWebSocket, disconnectWebSocket, sdk, waitForWsOpen } from '@/services/api-client'
import { useRoomPlayers } from '@/hooks/useRoomPlayers'
import { useRuleset } from '@/hooks/useRuleset'

const SHEET_PAGES = [
  { key: 'info', label: '调查员' },
  { key: 'skills', label: '技能' },
  { key: 'background', label: '背景' },
] as const

/** 🔴 弹层高度是常量，内容超出在里面滚。
 *  同 RoomPage 的 `PANEL_HEIGHT_VH`：手机端上高度随内容跳很难受，切 tab
 *  （调查员 ↔ 技能 ↔ 背景）时尤其明显——三页内容长度差得远。 */
const SHEET_HEIGHT_VH = 74

/** 一格属性/技能：值 + 各难度档的门槛（COC7 的「值 / 半 / 五分之一」）。
 *
 * 🔴 除数**来自后端 ruleset**（`successTiers`），不在前端写 2 和 5——
 * 服务端判定检定成功等级用的是同一份除数（`keeper/primitives/dice.py`），
 * 抄第二份必然在改规则时漏掉一处。
 * 没声明分档的规则系统就不画这一列：那不是兜底，是这套规则本来就没有难度档。
 */
function TierValues({
  value,
  tiers,
  className = '',
}: {
  value: number
  tiers: { id: string; label: string; divisor: number }[]
  className?: string
}) {
  if (tiers.length === 0) return null
  return (
    <span className={`font-mono text-[10.5px] text-ink-soft ${className}`}>
      {tiers.map((t) => Math.floor(value / t.divisor)).join(' / ')}
    </span>
  )
}

/** 调查员档案：借的是**表单这个体裁**（打字机标签压在填空线上、分区横线、
 *  属性三格、本职技能实心方块），不是任何一份出版物的版式。 */
function InvestigatorSheet({
  character,
  onClose,
  onRegenerate,
  regenerating,
  regenerateError,
}: {
  character: NonNullable<ReturnType<typeof useCharacterStore.getState>['character']>
  onClose: () => void
  onRegenerate: () => void
  regenerating: boolean
  regenerateError: string
}) {
  const [page, setPage] = useState<(typeof SHEET_PAGES)[number]['key']>('info')
  // 二次点击确认：走向导手写过背景的人点下去会被覆盖，而移动端弹 confirm
  // 体验差。第一次点变成确认态，再点才真执行。
  const [confirmingRegen, setConfirmingRegen] = useState(false)
  const { ruleset } = useRuleset()
  const occupation = character.info.occupationId
    ? ruleset?.occupations.find((o) => o.id === character.info.occupationId)
    : null
  const tiers = ruleset?.successTiers ?? []
  // 本职技能：职业的固定技能表。自选槽占用的那几项后端只在建卡预览里算，
  // 角色卡读接口没有，所以标不出来——宁可少标也不乱标。
  const occupationSkillIds = new Set(occupation?.skillIds ?? [])

  useEffect(() => {
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = ''
    }
  }, [])

  const detail = character.backgroundDetail
  const filledDetail = BACKGROUND_DETAIL_FIELDS.filter((f) => (detail?.[f.key] ?? '').trim())

  return (
    <>
      <div className="fixed inset-0 bg-black/60 z-30 animate-fade-in" onClick={onClose} />
      {/* 🔴 `theme-paper`：CSS 变量沿 DOM 继承，光是"不加 theme-coc"不够——
          祖先上有就照样生效，症状是牛皮纸上一片空白。 */}
      <div
        className="theme-paper paper-grain fixed inset-x-0 bottom-0 z-40 bg-dossier text-ink flex flex-col animate-slide-up max-w-[430px] mx-auto shadow-[0_-1px_0_rgba(255,255,255,.22),0_-3px_10px_rgba(0,0,0,.35)] border-t-[3px] border-brass-dark"
        style={{ height: `${SHEET_HEIGHT_VH}vh` }}
      >
        {/* 标签舌：档案最好认的特征 */}
        <span className="tab-flap absolute left-[26px] -top-[19px] typed text-[10.5px] px-3.5 pt-[5px] pb-1 bg-brass-dark text-dossier">
          调查员
        </span>

        {/* 收起键自己占一行、钉在顶部：绝对定位的控件不知道内容长什么样，必然撞 */}
        <div className="flex-none flex items-center justify-between px-4 pt-2.5 pb-1.5">
          <span className="text-[13px] font-bold text-ink">{character.info.name}</span>
          <button
            onClick={onClose}
            className="typed flex items-center gap-1 text-[10.5px] text-ink-soft active:text-ink px-1 py-0.5"
          >
            收起
            <X className="w-3 h-3" strokeWidth={2.5} />
          </button>
        </div>

        {/* 分页：表单的页签，不是圆角胶囊 */}
        <div className="flex-none flex px-4 border-b border-ink/25">
          {SHEET_PAGES.map((p) => (
            <button
              key={p.key}
              onClick={() => setPage(p.key)}
              className={`text-[12px] px-3 pt-1.5 pb-1 border-b-2 -mb-px transition-colors ${
                page === p.key
                  ? 'border-brass-dark text-ink font-bold'
                  : 'border-transparent text-ink-soft'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3">
          {page === 'info' && (
            <>
              <SheetBand title="调查员档案" note="COC 7e" />
              <div className="flex flex-col gap-2 mb-3.5">
                <div className="flex gap-3">
                  <FillField label="姓名" value={character.info.name} grow={2} />
                  <FillField label="年龄" value={character.info.age || '—'} mono />
                </div>
                <div className="flex gap-3">
                  <FillField label="职业" value={occupation?.name ?? '未选择'} grow={2} />
                  <FillField label="性别" value={character.info.gender || '—'} />
                </div>
                <div className="flex gap-3">
                  <FillField label="居住地" value={character.info.residence || '—'} />
                  <FillField label="出生地" value={character.info.birthplace || '—'} />
                </div>
              </div>

              <SheetBand
                title="属性"
                note={tiers.length ? `值 · ${tiers.map((t) => t.label).join(' · ')}` : undefined}
              />
              <div className="grid grid-cols-3 gap-1.5 mb-3.5">
                {(ruleset?.attributes ?? []).map((attribute) => (
                  <div key={attribute.key} className="border border-ink/45 bg-white/15">
                    <div className="typed text-[10.5px] text-center py-[2px] bg-ink/[0.13] border-b border-ink/30 text-ink">
                      {attribute.key}
                    </div>
                    <div className="flex items-baseline justify-center gap-1.5 py-1">
                      <span className="font-mono text-[15px] font-bold text-ink">
                        {character.attr[attribute.key]}
                      </span>
                      <TierValues value={character.attr[attribute.key] ?? 0} tiers={tiers} />
                    </div>
                  </div>
                ))}
              </div>

              <SheetBand title="状态" />
              <div className="flex gap-1.5">
                {[
                  { k: 'HP', v: `${character.derived.hp}`, c: 'text-[#3d6b2f]' },
                  { k: 'SAN', v: `${character.derived.san}`, c: 'text-[#57407e]' },
                  { k: 'MP', v: `${character.derived.mp}`, c: 'text-[#3a5a7a]' },
                  { k: 'DB', v: character.derived.db, c: 'text-ink' },
                  { k: 'MOV', v: `${character.derived.move}`, c: 'text-ink' },
                ].map((v) => (
                  <div key={v.k} className="flex-1 border-[1.5px] border-ink/50 bg-white/15">
                    <div className="typed text-[10.5px] text-center py-[2px] border-b border-ink/30 text-ink-soft">
                      {v.k}
                    </div>
                    <div className={`text-center font-mono text-[17px] font-bold py-0.5 ${v.c}`}>
                      {v.v}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          {page === 'skills' && (
            <>
              <SheetBand title="技能" note={`共 ${ruleset?.skills.length ?? 0} 项`} />
              <div className="flex items-center gap-1.5 mb-2 text-[10.5px] text-ink-soft">
                <span className="w-[7px] h-[7px] border border-ink/55 bg-ink inline-block" />
                本职技能
                {tiers.length > 0 && (
                  <span className="ml-auto typed">值 / {tiers.map((t) => t.label).join(' / ')}</span>
                )}
              </div>
              {/* 🔴 两栏密排的清单，不是一条条进度条：62 项技能用进度条会拖成
                  一条看不完的走廊，而表单本来就是密排的。 */}
              <div className="grid grid-cols-2 gap-x-3">
                {(ruleset?.skills ?? [])
                  .map((skill) => ({ skill, value: character.skillFinalValues?.[skill.id] ?? 0 }))
                  .sort((a, b) => b.value - a.value)
                  .map(({ skill, value }) => (
                    <div
                      key={skill.id}
                      className="flex items-center gap-1.5 py-[3px] border-b border-dotted border-ink/30"
                    >
                      <span
                        className={`w-[7px] h-[7px] flex-none border border-ink/55 ${
                          occupationSkillIds.has(skill.id) ? 'bg-ink' : ''
                        }`}
                      />
                      <span className="flex-1 min-w-0 truncate text-[11.5px] text-ink">
                        {skill.name}
                      </span>
                      <span className="font-mono text-[11.5px] font-bold text-ink">{value}</span>
                      <TierValues value={value} tiers={tiers} className="min-w-[26px] text-right" />
                    </div>
                  ))}
              </div>
            </>
          )}

          {page === 'background' && (
            <>
              {/* 背景故事的八个具名栏：后端一直在存这八个字段，此前这一屏
                  把它们拍成了一整段。只渲染填了的，空栏不占地方。 */}
              {filledDetail.length > 0 && (
                <>
                  <SheetBand title="背景故事" />
                  <div className="flex flex-col gap-2 mb-3.5">
                    {filledDetail.map((f) => (
                      <div key={f.key}>
                        <div className="typed text-[10.5px] text-ink-soft mb-0.5">{f.label}</div>
                        <p className="notepaper border border-ink/28 px-2 text-[12px] text-ink whitespace-pre-wrap">
                          {detail?.[f.key]}
                        </p>
                      </div>
                    ))}
                  </div>
                </>
              )}

              <SheetBand title="装备" />
              <p className="notepaper border border-ink/28 px-2 mb-3.5 text-[12px] text-ink whitespace-pre-wrap">
                {character.equipment || '暂未填写'}
              </p>

              <SheetBand title="履历" />
              <p className="notepaper border border-ink/28 px-2 text-[12px] text-ink whitespace-pre-wrap">
                {character.background || '暂未填写'}
              </p>
              {/* exec/20 §1.9 定的方向：内容质量不该由代码判，该给玩家一个
                  重摇的按钮，让人来判。只换过去，属性/技能一个都不动。 */}
              <button
                onClick={() => {
                  if (regenerating) return
                  if (!confirmingRegen) {
                    setConfirmingRegen(true)
                    return
                  }
                  setConfirmingRegen(false)
                  onRegenerate()
                }}
                disabled={regenerating}
                className={`cut-corner mt-2 w-full flex items-center justify-center gap-1.5 px-4 py-2 text-[12px] font-semibold transition-all ${
                  regenerating
                    ? 'border border-ink/30 text-ink-soft cursor-not-allowed'
                    : confirmingRegen
                      ? 'bg-brass-dark border border-brass-dark text-dossier active:scale-[0.97]'
                      : 'border border-brass-dark text-brass-dark bg-white/20 active:bg-brass-dark active:text-dossier active:scale-[0.97]'
                }`}
              >
                <RefreshCw className={`w-3.5 h-3.5 ${regenerating ? 'animate-spin' : ''}`} />
                {regenerating
                  ? '正在重写…'
                  : confirmingRegen
                    ? '确定？当前背景会被替换'
                    : '换一段过去'}
              </button>
              {confirmingRegen && !regenerating && (
                <p className="text-[11px] text-ink-soft text-center mt-1.5">
                  只换背景故事，属性、技能、职业都不变
                </p>
              )}
              {regenerateError && (
                <p className="text-[11px] text-rust-dark text-center mt-1.5">{regenerateError}</p>
              )}

              <SheetBand title="备注" className="mt-3.5" />
              <p className="notepaper border border-ink/28 px-2 text-[12px] text-ink whitespace-pre-wrap">
                {character.notes || '暂未填写'}
              </p>
            </>
          )}
        </div>
      </div>
    </>
  )
}

/** 分区横线：表单靠它断开段落，而不是靠一堆卡片。 */
function SheetBand({
  title,
  note,
  className = '',
}: {
  title: string
  note?: string
  className?: string
}) {
  return (
    <div
      className={`flex items-baseline gap-2 pb-1 mb-2 border-b-[1.5px] border-ink/40 ${className}`}
    >
      <span className="text-[12.5px] font-bold tracking-[0.06em] text-ink">{title}</span>
      {note && <span className="typed ml-auto text-[10.5px] text-ink-soft">{note}</span>}
    </div>
  )
}

/** 填空行：标签在左，值坐在实线上。 */
function FillField({
  label,
  value,
  grow = 1,
  mono = false,
}: {
  label: string
  value: string | number
  grow?: number
  mono?: boolean
}) {
  return (
    <div className="flex items-end gap-1.5" style={{ flex: grow }}>
      <span className="typed text-[10.5px] text-ink-soft whitespace-nowrap pb-[3px]">{label}</span>
      <span
        className={`fill-line flex-1 min-w-0 truncate px-0.5 pb-[1px] text-[13.5px] font-semibold text-ink ${
          mono ? 'font-mono' : ''
        }`}
      >
        {value}
      </span>
    </div>
  )
}

// 第二个等待界面：每个人各自建完卡之后，先看看队友是不是也都建完了，
// 全员建完卡房主才能真正开始游戏（发 game.start），其他人靠轮询房间
// phase 变成 InGame 各自跟上、一起进入聊天室。
export default function CharacterReadyPage() {
  const navigate = useNavigate()
  const [showSelfSheet, setShowSelfSheet] = useState(false)
  const [starting, setStarting] = useState(false)
  // 存进卡库（我的常用角色卡）。存的是**哪一张**要记住，因为存卡必须可撤——
  // 点错了不该逼玩家自己摸到卡库里去删。
  //
  // 仍然只是这一屏的即时反馈：重进页面会回到"存卡"（那时 `fromTemplate` 会
  // 接管"这张卡本来就来自卡库"那一种）。
  const [savingTemplate, setSavingTemplate] = useState(false)
  const [savedTemplateId, setSavedTemplateId] = useState<string | null>(null)
  const [templateError, setTemplateError] = useState('')
  const roomId = useRoomStore((s) => s.roomId)
  const selfPlayerId = useRoomStore((s) => s.playerId)
  const cachedCharacter = useCharacterStore((s) =>
    roomId ? s.getForRoom(roomId, selfPlayerId) : null
  )
  const characterId = useRoomStore((s) => s.characterId)
  const { ruleset: readyRuleset } = useRuleset()

  // 角色卡以**后端**为准，本地缓存只作首屏占位（issue #96）。
  //
  // 之前这里只读 localStorage：清掉缓存（或换浏览器）后，明明后端有这张卡，
  // 页面却显示成"还没建卡"。现在有了 GET 端点，就该以后端那份为准——本地缓存
  // 保留是为了拉取回来之前不闪空白，不是权威源。
  const [remoteCharacter, setRemoteCharacter] = useState<typeof cachedCharacter>(null)
  // 这张卡是不是从卡库拿的。是的话它**已经在卡库里**，不该请玩家再存一遍——
  // 存了只会得到一张一模一样的（2026-08-13 真人反馈）。
  const [fromTemplate, setFromTemplate] = useState(false)
  useEffect(() => {
    if (!roomId || !characterId || !readyRuleset) return
    let cancelled = false
    fetchCharacter(roomId, characterId)
      .then((saved) => {
        if (cancelled) return
        setRemoteCharacter(toCompletedCharacter(saved, readyRuleset))
        setFromTemplate(saved.basedOnTemplateId != null)
      })
      .catch(() => {
        // 拉不到就沿用本地缓存（比如还没建过卡），不打断这个页面。
      })
    return () => {
      cancelled = true
    }
  }, [roomId, characterId, readyRuleset])

  const [regeneratingBackground, setRegeneratingBackground] = useState(false)
  const [regenerateBackgroundError, setRegenerateBackgroundError] = useState('')
  const handleSaveTemplate = async () => {
    if (!characterId) return
    setSavingTemplate(true)
    setTemplateError('')
    try {
      // 卡库里的名字用角色名——玩家找的是"我那个记者"，不是一串日期。
      const saved = await saveAsTemplate(characterId, character?.info.name || '我的调查员')
      // 🔴 记住存出来的是哪张，才能撤：存卡此前是**单向的**，点错了只能自己
      // 摸到卡库里去删（2026-08-13 真人反馈）。
      setSavedTemplateId(saved.templateId)
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : '存进卡库失败')
    } finally {
      setSavingTemplate(false)
    }
  }

  /** 撤销刚才那次存卡：把它从卡库里删掉，按钮回到"存卡"。 */
  const handleUndoSaveTemplate = async () => {
    if (!savedTemplateId) return
    setSavingTemplate(true)
    setTemplateError('')
    try {
      await deleteMyTemplate(savedTemplateId)
      setSavedTemplateId(null)
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : '撤销失败')
    } finally {
      setSavingTemplate(false)
    }
  }

  const handleRegenerateBackground = async () => {
    if (!roomId || !characterId || !readyRuleset) return
    setRegeneratingBackground(true)
    setRegenerateBackgroundError('')
    try {
      const updated = await regenerateBackground(roomId, characterId)
      setRemoteCharacter(toCompletedCharacter(updated, readyRuleset))
    } catch {
      // 后端在生成服务不可用时返回 503（而不是静默保持原样）——这里如实告诉他，
      // 否则他会以为按钮坏了然后一直点。
      setRegenerateBackgroundError('这次没写出来，稍后再试试')
    } finally {
      setRegeneratingBackground(false)
    }
  }

  const character = remoteCharacter ?? cachedCharacter
  const roomCode = useRoomStore((s) => s.roomCode)
  const isHost = useRoomStore((s) => s.isHost)
  const playerId = useRoomStore((s) => s.playerId)
  const reconnectToken = useRoomStore((s) => s.reconnectToken)
  const nickname = useAuthStore((s) => s.nickname)
  const hasCharacter = character !== null
  const info = useRoomPlayers(roomCode)
  const players = info?.players ?? []
  const allHaveCharacters = players.length > 0 && players.every((p) => p.hasCharacter)
  const advancedRef = useRef(false)
  // 还没坐满的位置，直接在登记表上画出来——几人局还差几个人一眼看得见
  const emptySeats = Math.max(0, (info?.maxPlayers ?? 0) - players.length)

  // ★ 房主点"开始游戏"之后，后端 _on_game_start 会把房间 phase 改成
  // InGame——其他玩家没有 WS 广播可用，只能靠轮询这个字段发现"游戏真的开始
  // 了"，然后自己跟上进 /room，而不是自己一厢情愿地提前进去。
  useEffect(() => {
    if (info?.phase === 'InGame' && !advancedRef.current) {
      advancedRef.current = true
      navigate('/room/play')
    }
  }, [info?.phase, navigate])

  const handleStartGame = async () => {
    if (!isHost || !playerId || !roomId) return
    setStarting(true)
    try {
      // ★ 这个页面从来没有主动建立过 WS 连接（只有 LobbyPage 会连）——如果
      // 刷新过页面、或者从没经过 Lobby 直接落到这里，connectWebSocket 拿到
      // 的连接是关闭的，startGame 会静默丢弃 game.start，后端 phase
      // 永远停在 Building，其他玩家会一直卡在轮询里。这里跟 RoomPage 一样，
      // 发 game.start 前先确保连接是通的、且已经 room.join 过（对已经连过
      // 的情况是幂等空操作）。
      const ws = connectWebSocket(roomId)
      await waitForWsOpen(ws)
      sdk.roomSocket.joinRoom(playerId, {
        reconnectToken: reconnectToken || '',
        roomCode,
        nickname: nickname || '玩家',
      })
      sdk.roomSocket.startGame(playerId)
    } catch {
      setStarting(false)
      return
    }
    // ★ 房主要立刻本地跳转，不能也靠轮询 phase 等——AI 生成开场旁白要好几秒，
    // 但如果房主自己还要等下一次轮询（最多 3 秒）才进 RoomPage，RoomPage
    // 还没挂载、没人订阅 onWsMessage，narration.push 广播到达时就直接被
    // 丢弃收不到了。访客那边则没有这个问题：靠轮询进入的等待时间通常短于
    // AI 生成旁白的时间，RoomPage 大概率已经挂载好在等了。
    advancedRef.current = true
    navigate('/room/play')
  }

  const handleEditCharacter = () => {
    navigate('/room/character', { state: { fromCharacterReady: true } })
  }

  const handleGoBack = () => {
    disconnectWebSocket()
    navigate('/home')
  }

  return (
    // 🔴 `theme-coc`：卷宗主题的作用域边界，同 RoomPage。桌面材质（木纹 /
    // 台灯 / 刻在桌上的符环）都挂在这一层，纸放上去才有"摊在桌上"的关系。
    //
    // 🔴 `bg-card` 不能少：木纹是 `multiply` 混合上去的，**底下没有颜色就
    // 等于没有**。漏掉它的真机症状是整页停在外层的浅底上，牛皮纸浮在白纸上，
    // 色差突兀——材质类名齐全也救不回来，因为它们没有可乘的底色。
    <div className="theme-coc desk-grain desk-lamp desk-sigil bg-card animate-screen-in min-h-full px-5 pt-6 pb-8 flex flex-col relative">
      <button
        onClick={handleGoBack}
        className="cut-corner w-[34px] h-[34px] bg-input border border-border-mid flex items-center justify-center flex-shrink-0 active:bg-panel active:scale-[0.94] transition-all duration-150 mb-3 relative z-10"
      >
        <ArrowLeft className="w-[18px] h-[18px] text-text-body" strokeWidth={2.5} />
      </button>

      {/* 房间号 = 钢印牌。原来是虚线框，看着像个还没填的输入框 */}
      <div className="text-center relative z-10">
        <span className="typed block text-[10.5px] text-text-muted mb-1.5">卷宗编号</span>
        <span className="plate inline-block px-[18px] pt-[7px] pb-1.5 bg-input border border-brass-dark font-mono text-[25px] font-bold text-brass-bright tracking-[0.28em] indent-[0.28em]">
          {roomCode || '------'}
        </span>
      </div>
      <p className="text-center text-[11.5px] text-text-body leading-relaxed mt-2 mb-4 relative z-10">
        人物卡准备 · 等待所有玩家创建角色
        {info && (
          <>
            <br />
            <span className="text-brass-bright font-mono">{players.length}</span> / {info.maxPlayers}{' '}
            人已加入
          </>
        )}
      </p>

      {/* 🔴 一张登记表：全员在同一张纸上，每人一行。空位直接画出来。
          `theme-paper` 必须挂——变量沿 DOM 继承，祖先的 theme-coc 会一路生效。 */}
      <div className="theme-paper paper-grain relative z-10 bg-dossier text-ink shadow-[0_1px_0_rgba(0,0,0,.34),0_10px_16px_-8px_rgba(0,0,0,.6)]">
        <div className="typed flex items-center px-3 pt-2 pb-1.5 border-b-[1.5px] border-ink/40 text-[10.5px] text-ink-soft">
          <span className="flex-1">调查员登记表</span>
          <span className="font-mono">{roomCode || '------'}</span>
        </div>

        {players.length === 0 && (
          <div className="text-center py-6 text-[11.5px] text-ink-soft">正在获取房间成员…</div>
        )}

        {players.map((p) => {
          const isSelf = p.playerId === playerId
          return (
            <div
              key={p.playerId}
              className="flex items-center gap-2.5 px-3 py-2.5 border-b border-ink/20 last:border-b-0"
            >
              <div
                className={`w-[34px] h-[34px] flex-none flex items-center justify-center text-[15px] border bg-ink/[0.08] ${
                  p.hasCharacter ? 'border-ink/35' : 'border-dashed border-ink/35 text-ink-soft'
                }`}
              >
                {p.hasCharacter ? '🔍' : '○'}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[13.5px] font-bold text-ink truncate">
                  {p.nickname}
                  {isSelf && '（你）'}
                </div>
                <div className="text-[11px] text-ink-soft truncate">
                  {isSelf && hasCharacter
                    ? `人物卡：${character!.info.name}`
                    : p.hasCharacter
                      ? '已完成建卡'
                      : '尚未创建人物卡'}
                </div>
                {/* 存卡失败要说出来——静默失败等于玩家以为存进去了，
                    下一局打开卡库发现没有。 */}
                {isSelf && templateError && (
                  <div className="text-[11px] text-rust-dark truncate">{templateError}</div>
                )}
              </div>
              {isSelf ? (
                <div className="flex items-center gap-1.5">
                  {hasCharacter ? (
                    <>
                      <button
                        onClick={() => setShowSelfSheet(true)}
                        className="cut-corner text-[11px] font-semibold px-2 py-1 border border-brass-dark text-brass-dark bg-white/25 flex items-center gap-1 active:scale-[0.95] transition-all whitespace-nowrap"
                      >
                        <Eye className="w-3 h-3" /> 查看
                      </button>
                      <button
                        onClick={handleEditCharacter}
                        className="cut-corner text-[11px] font-semibold px-2 py-1 border border-ink/35 text-ink-soft bg-white/15 active:scale-[0.95] transition-all whitespace-nowrap"
                      >
                        编辑
                      </button>
                      {/* 存进卡库：这一屏的卡已经是 complete 的完整态，
                          而向导最后一步那张还是 draft——存早了会存进半截数据。

                          三种状态：
                          ①这张卡本来就是从卡库拿的 → 不给存（存了只是复制一张
                            一模一样的），按钮说明它已经在库里；
                          ②刚存过 → 给「撤销」，存卡不能是单向的；
                          ③其余 → 「存卡」。 */}
                      {fromTemplate ? (
                        <span
                          title="这张卡就是从卡库里拿的，已经在库里了"
                          className="cut-corner text-[11px] font-semibold px-2 py-1 border border-ink/25 text-ink-soft/70 bg-white/10 flex items-center gap-1 whitespace-nowrap"
                        >
                          <BookmarkCheck className="w-3 h-3" /> 在卡库
                        </span>
                      ) : savedTemplateId ? (
                        <button
                          onClick={() => void handleUndoSaveTemplate()}
                          disabled={savingTemplate}
                          title="从卡库里撤掉刚存的那张"
                          className="cut-corner text-[11px] font-semibold px-2 py-1 border border-ink/35 text-ink-soft bg-white/15 flex items-center gap-1 active:scale-[0.95] transition-all whitespace-nowrap disabled:opacity-50"
                        >
                          <BookmarkCheck className="w-3 h-3" />
                          {savingTemplate ? '撤…' : '已存 · 撤销'}
                        </button>
                      ) : (
                        <button
                          onClick={() => void handleSaveTemplate()}
                          disabled={savingTemplate}
                          title="存进我的常用卡"
                          className="cut-corner text-[11px] font-semibold px-2 py-1 border border-ink/35 text-ink-soft bg-white/15 flex items-center gap-1 active:scale-[0.95] transition-all whitespace-nowrap disabled:opacity-50"
                        >
                          <BookmarkPlus className="w-3 h-3" />
                          {savingTemplate ? '存…' : '存卡'}
                        </button>
                      )}
                    </>
                  ) : (
                    <button
                      onClick={handleEditCharacter}
                      className="cut-corner text-[11px] font-semibold px-2.5 py-1 bg-brass-dark text-dossier flex items-center gap-1 active:scale-[0.95] transition-all whitespace-nowrap"
                    >
                      <UserPlus className="w-3 h-3" /> 创建人物卡
                    </button>
                  )}
                </div>
              ) : (
                // 队友只看得到"建完了没有"——角色卡内容是私密的。
                // 状态用盖章表达，不是又一行小字。
                <span
                  className={`stamped typed text-[10px] font-bold px-1.5 py-0.5 ${
                    p.hasCharacter ? 'text-[#3d6b2f]' : 'text-[#8a6a2e] border-dashed'
                  }`}
                >
                  {p.hasCharacter ? '已备案' : '待填'}
                </span>
              )}
            </div>
          )
        })}

        {Array.from({ length: emptySeats }).map((_, i) => (
          <div
            key={`empty-${i}`}
            className="flex items-center gap-2.5 px-3 py-2.5 border-b border-ink/20 last:border-b-0"
          >
            <div className="w-[34px] h-[34px] flex-none flex items-center justify-center border border-dashed border-ink/25 text-ink-faint text-[15px]">
              ○
            </div>
            <span className="text-[11.5px] text-ink-soft">—— 空位 ——</span>
          </div>
        ))}
      </div>

      <div className="flex-1" />

      <p className="text-center text-[11.5px] text-text-body leading-relaxed mt-6 mb-3 relative z-10">
        {isHost
          ? '将房间号分享给好友，让他们加入游戏并创建角色'
          : allHaveCharacters
            ? '全员已完成建卡，等待房主开始游戏…'
            : '等待所有玩家完成建卡'}
      </p>

      {/* 只有房主能真正开始游戏，且要等全员都建完卡才能点 */}
      {isHost ? (
        <button
          onClick={handleStartGame}
          disabled={!allHaveCharacters || starting}
          className={`relative z-10 w-full py-3.5 text-[14.5px] font-bold tracking-[0.22em] indent-[0.22em] flex items-center justify-center gap-2 transition-all ${
            !allHaveCharacters || starting
              ? 'bg-panel border border-border-mid text-text-dim'
              : 'seal bg-brass-dark border border-brass text-text-primary active:translate-y-[1px]'
          }`}
        >
          <Swords className="w-4 h-4" />
          {starting ? '进入中…' : '开始游戏'}
        </button>
      ) : (
        <div className="relative z-10 w-full py-3.5 bg-panel border border-border-mid text-text-dim text-[14.5px] font-bold tracking-[0.22em] indent-[0.22em] text-center">
          等待房主开始游戏…
        </div>
      )}

      {showSelfSheet && character && (
        <InvestigatorSheet
          character={character}
          onClose={() => setShowSelfSheet(false)}
          onRegenerate={handleRegenerateBackground}
          regenerating={regeneratingBackground}
          regenerateError={regenerateBackgroundError}
        />
      )}
    </div>
  )
}
