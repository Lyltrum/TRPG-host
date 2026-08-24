import { useNavigate } from 'react-router-dom'
import { useEffect, useState, useRef, lazy, Suspense } from 'react'
import { UserPlus, ArrowLeft, Bot, Plus, Share2 } from 'lucide-react'
// 🔴 懒加载：二维码库有 28KB，而大厅**每次进房都要加载**、邀请只点一次。
// 不拆的话所有人每次都为一个偶尔用的功能付这份流量。
const InviteSheet = lazy(() => import('@/shared/components/InviteSheet'))
import { useRoomStore } from '@/stores/room-store'
import { useAuthStore } from '@/stores/auth-store'
import { connectWebSocket, sdk, onWsMessage, waitForWsOpen, disconnectWebSocket, friendlyErrorMessage } from '@/services/api-client'
import {
  startStory,
  addAiPlayer,
  disbandRoom,
  kickPlayer,
  transferHost,
  updateRoomSettings,
} from '@/services/room'
import { useRoomPlayers } from '@/hooks/useRoomPlayers'

// 第一个等待界面：等所有玩家进入房间、都标记"已就绪"，才能一起往下走到
// 背景介绍 + 建卡（见需求：不论房主还是访客，全员到齐才能开始）。
export default function LobbyPage() {
  const navigate = useNavigate()

  // ★ 不要用 useRoomStore(s => ({...})) 这种每次渲染都新建对象的写法——
  // Zustand 的 useSyncExternalStore 会因为引用不相等而判定"变了"，触发无限重渲染。
  const roomId = useRoomStore((s) => s.roomId)
  const isHost = useRoomStore((s) => s.isHost)
  const roomCode = useRoomStore((s) => s.roomCode)
  const playerId = useRoomStore((s) => s.playerId)
  const reconnectToken = useRoomStore((s) => s.reconnectToken)
  const nickname = useAuthStore((s) => s.nickname)
  const [inviting, setInviting] = useState(false)
  const [ready, setReady] = useState(false)
  const [joined, setJoined] = useState(false)
  const [error, setError] = useState('')
  const [confirmLeave, setConfirmLeave] = useState(false)
  const info = useRoomPlayers(roomCode)
  const advancedRef = useRef(false)

  useEffect(() => {
    if (!roomId || !playerId) return
    let cancelled = false

    const off = onWsMessage((envelope) => {
      if (envelope.type === 'session.bound' && !cancelled) {
        setJoined(true)
      }
    })

    const ws = connectWebSocket(roomId)
    waitForWsOpen(ws)
      .then(() => {
        if (cancelled) return
        sdk.roomSocket.joinRoom(playerId, {
          reconnectToken: reconnectToken || '',
          roomCode,
          nickname: nickname || '玩家',
        })
      })
      .catch(() => setError('WebSocket 连接失败'))

    return () => {
      cancelled = true
      off()
      // ★ 这里故意不 disconnectWebSocket()——连接要跨 LobbyPage→RoomPage 导航
      // 保持不断。connectWebSocket 本身是幂等的（同一 roomId 直接复用）。
    }
  }, [roomId, playerId, roomCode, nickname, reconnectToken])

  const players = info?.players ?? []
  // 房主在这个页面上没有"标记已就绪"按钮（只有"开始游戏"），他们用点击
  // 开始游戏本身表达意愿——所以判断"全员就绪"时要把房主排除在外，只看
  // 访客，否则房主自己的 ready 永远是 false，"开始游戏"按钮永远点不了。
  const nonHostPlayers = players.filter((p) => !p.isHost)
  const allReady = players.length > 0 && nonHostPlayers.every((p) => p.ready)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState('')
  const emptySeats = Math.max(0, (info?.maxPlayers ?? 0) - players.length)

  // ★ 全员就绪只是"可以开始"的前提，不代表自动开始——房主必须主动点"开始
  // 游戏"才真正推进（见反馈：不应该默认自动跳转）。访客端没有这个按钮，
  // 靠轮询 storyStarted 标记跟进，这个标记只有房主点击后才会被置位。
  useEffect(() => {
    if (isHost) return
    if (info?.storyStarted && !advancedRef.current) {
      advancedRef.current = true
      navigate('/room/story')
    }
  }, [info?.storyStarted, isHost, navigate])

  const handleStartStory = async () => {
    if (!roomId || !allReady) return
    setStarting(true)
    // 失败必须复位 starting、并且把原因显示出来。原来这里既没有 catch 也没有
    // finally——后端一旦拒绝（比如房间已经过了大厅阶段会返回 409），按钮就永久
    // 卡在「开始中…」，用户既走不下去也看不到任何原因，只能刷新页面。
    try {
      setStartError('')
      await startStory(roomId)
      advancedRef.current = true
      navigate('/room/story')
    } catch (err) {
      setStartError(friendlyErrorMessage(err, '开始游戏失败'))
    } finally {
      setStarting(false)
    }
  }

  // 人不齐时房主点空位补一个 AI 队友（exec/21）。列表靠 useRoomPlayers 轮询
  // 刷新，所以这里不手动改本地状态，只在等待期间禁用按钮避免连点加出两个。
  const [addingAi, setAddingAi] = useState(false)
  const handleAddAiPlayer = async () => {
    if (!roomId || addingAi) return
    setAddingAi(true)
    try {
      setError('')
      await addAiPlayer(roomId)
    } catch (err) {
      setError(friendlyErrorMessage(err, '加 AI 队友失败'))
    } finally {
      setAddingAi(false)
    }
  }

  const toggleReady = () => {
    if (!playerId) return
    const next = !ready
    setReady(next)
    sdk.roomSocket.setReady(playerId, { ready: next })
  }

  const handleLeave = async () => {
    // ★ 不能让"没有 playerId 就直接 return"卡死用户——刷新页面等场景下
    // room-store 可能还没恢复完，但用户始终要有办法离开这个页面（见
    // 2026-07-13 测试报告 P0：返回按钮失效导致的死锁）。
    if (playerId && !confirmLeave) {
      setConfirmLeave(true)
      return
    }
    // 🔴 房主这条确认一直写着「所有成员将被移出」，而代码只是 navigate 回首页
    // ——房间还活着、别人还在里面等。文案说的事现在真的会发生了。
    if (roomId && playerId) {
      try {
        // 🔴 非房主这条以前**一个请求都不发**，只是 navigate 回首页——人已经走了，
        // 大厅里还挂着他的名字和一张卡，剩下的人以为在等他，而"全员就绪"永远
        // 凑不齐。真机撞到过。
        await (isHost ? disbandRoom(roomId) : kickPlayer(roomId, playerId))
      } catch (err) {
        setError(friendlyErrorMessage(err, isHost ? '解散房间失败' : '离开房间失败'))
        return
      }
    }
    if (playerId) disconnectWebSocket()
    navigate('/home')
  }

  // 房主点某个成员 → 一张便条，里面是对他的操作。做成便条而不是行内按钮：
  // 375 宽的行里已经有头像、昵称、身份、就绪盖章，再塞两个按钮会挤成一团。
  const [acting, setActing] = useState<{ playerId: string; nickname: string } | null>(null)
  const [memberBusy, setMemberBusy] = useState(false)

  const runMemberAction = async (action: () => Promise<void>, fallback: string) => {
    if (memberBusy) return
    setMemberBusy(true)
    try {
      setError('')
      await action()
      setActing(null)
    } catch (err) {
      setError(friendlyErrorMessage(err, fallback))
    } finally {
      setMemberBusy(false)
    }
  }

  // 人数上限。中途加入放开之后，最常撞上的就是"位置不够了"。
  const [seatBusy, setSeatBusy] = useState(false)
  /**
   * 「骰子在桌上」（`exec/46` B5）。
   *
   * 🔴 它记的不是一个偏好，是**这一局的物理事实**：大家围坐一桌、手边有实体
   * 骰子。开着之后玩家可以在掷骰卡片上报自己掷出的出目，而**判定仍然全在
   * 后端**（要不要检定、目标值、算不算成功、幸运能不能补）——让出的只有随机数。
   *
   * 放在大厅而不是牌桌里：它是开局前定下来的一件事，而 `RoomPage` 只在挂载时
   * 读一次房间信息。中途改的话，别人要刷新才看得到。
   */
  const toggleManualRolls = async () => {
    if (!roomId || !info || seatBusy) return
    setSeatBusy(true)
    try {
      setError('')
      // 不用手动刷新：`useRoomPlayers` 每 3 秒轮询一次房间信息。
      await updateRoomSettings(roomId, info.maxPlayers, !info.allowManualRolls)
    } catch (err) {
      setError(friendlyErrorMessage(err, '改不了掷骰方式'))
    } finally {
      setSeatBusy(false)
    }
  }
  const changeSeats = async (delta: number) => {
    if (!roomId || !info || seatBusy) return
    setSeatBusy(true)
    try {
      setError('')
      await updateRoomSettings(roomId, info.maxPlayers + delta)
    } catch (err) {
      setError(friendlyErrorMessage(err, '改人数失败'))
    } finally {
      setSeatBusy(false)
    }
  }

  return (
    // 🔴 `bg-card` 是桌面本身的木色，不能少：木纹是 multiply 混合上去的，
    // 底下没有颜色等于没铺（准备页漏过一次，症状是纸浮在白底上）。
    <div className="theme-coc desk-grain desk-lamp desk-sigil bg-card animate-screen-in min-h-full px-5 pt-6 pb-8 flex flex-col relative">
      <button
        onClick={handleLeave}
        className="cut-corner w-[34px] h-[34px] bg-input border border-border-mid flex items-center justify-center flex-shrink-0 active:bg-panel active:scale-[0.94] transition-all duration-150 mb-3 relative z-10"
      >
        <ArrowLeft className="w-[18px] h-[18px] text-text-body" strokeWidth={2.5} />
      </button>

      {confirmLeave && (
        // 退出确认是**一张单页的纸**（`leaf`），不是页面里的一块卡片——
        // 它是压在桌上的一张便条，跟登记表不是同一件东西。
        <div className="theme-paper leaf paper-grain relative z-10 bg-book text-ink p-3.5 mb-3.5 border-l-[3px] border-l-rust">
          <p className="text-[12px] text-ink text-center mb-2.5 pl-2">
            {isHost ? '确定要解散房间吗？所有成员将被移出。' : '确定要离开房间吗？'}
          </p>
          <div className="flex gap-2 pl-2">
            <button onClick={() => setConfirmLeave(false)}
              className="cut-corner flex-1 py-2 border border-ink/35 text-ink-soft text-[12px] font-semibold bg-white/25 active:scale-[0.97]">
              取消
            </button>
            <button onClick={handleLeave}
              className="cut-corner flex-1 py-2 bg-rust-dark text-book text-[12px] font-semibold active:scale-[0.97]">
              {isHost ? '确认解散' : '确认离开'}
            </button>
          </div>
        </div>
      )}

      {/* 房间号 = 钢印牌。跟准备页同一枚，两屏之间不该换语言 */}
      <div className="text-center relative z-10">
        <span className="typed block text-[10.5px] text-text-muted mb-1.5">卷宗编号</span>
        <span className="plate inline-block px-[18px] pt-[7px] pb-1.5 bg-input border border-brass-dark font-mono text-[25px] font-bold text-brass-bright tracking-[0.28em] indent-[0.28em]">
          {roomCode || '------'}
        </span>
      </div>
      {/* 🔴 邀请入口挂在房间号旁边：房间号就是"怎么让别人进来"这个问题的
          旧答案，新答案该长在同一个位置上，而不是另开一屏。 */}
      <div className="flex justify-center mt-2.5 relative z-10">
        <button
          type="button"
          onClick={() => setInviting(true)}
          disabled={!roomCode}
          className="cut-corner flex items-center gap-1.5 px-3.5 py-1.5 bg-brass-dark text-book text-[12px] font-semibold disabled:opacity-50 active:scale-[0.97]"
        >
          <Share2 className="w-3.5 h-3.5" strokeWidth={2} />
          邀请朋友
        </button>
      </div>
      {inviting && roomCode && (
        <Suspense fallback={null}>
          <InviteSheet roomCode={roomCode} onClose={() => setInviting(false)} />
        </Suspense>
      )}
      <p className="text-center text-[11.5px] text-text-body leading-relaxed mt-2 mb-4 relative z-10">
        {joined ? '等待大厅 · 已连接' : '等待大厅 · 连接中…'}
        {info && (
          <>
            <br />
            <span className="text-brass-bright font-mono">{players.length}</span> / {info.maxPlayers}{' '}
            人已加入
            {/* 人数上限就长在"几人已加入"旁边——那是这个问题被问出来的地方 */}
            {isHost && (
              <span className="inline-flex items-center gap-1 ml-2 align-middle">
                <button
                  type="button"
                  onClick={() => changeSeats(-1)}
                  disabled={seatBusy || info.maxPlayers <= players.length}
                  className="cut-corner w-[20px] h-[20px] leading-none border border-brass-dark text-brass-bright disabled:opacity-35 active:scale-[0.92]"
                >
                  −
                </button>
                <button
                  type="button"
                  onClick={() => changeSeats(1)}
                  disabled={seatBusy || info.maxPlayers >= 20}
                  className="cut-corner w-[20px] h-[20px] leading-none border border-brass-dark text-brass-bright disabled:opacity-35 active:scale-[0.92]"
                >
                  ＋
                </button>
              </span>
            )}
          </>
        )}
      </p>

      {/* 「骰子在桌上」（`exec/46` B5）。只有房主能改，而且只在开局前——
          它是这一局的物理事实（大家手边有没有实体骰子），不是一个随时切换的
          偏好。**默认关着**：不开的房间，掷骰这条路一个字节都没变。 */}
      {isHost && info && (
        <button
          type="button"
          onClick={toggleManualRolls}
          disabled={seatBusy}
          className="leaf relative z-10 w-full text-left px-3.5 py-2.5 mb-3 disabled:opacity-50"
        >
          <span className="text-[12.5px] font-semibold text-ink">
            {info.allowManualRolls ? '🎲 用桌上的骰子' : '🎲 由系统掷骰'}
          </span>
          <span className="block text-[10.5px] text-ink-soft mt-0.5 leading-relaxed">
            {info.allowManualRolls
              ? '大家可以掷自己的骰子、把点数报进来。成功与否仍由系统判。'
              : '点一下改成「用桌上的骰子」——线下聚会时，让大家掷自己手边那颗。'}
          </span>
        </button>
      )}

      {/* 对某个成员的操作。同 `leaf`：压在桌上的一张便条，不是页面里的卡片 */}
      {acting && (
        <div className="theme-paper leaf paper-grain relative z-10 bg-book text-ink p-3.5 mb-3.5 border-l-[3px] border-l-brass-dark">
          <p className="text-[12px] text-ink mb-2.5 pl-2">
            对 <b>{acting.nickname}</b> 做点什么？
          </p>
          <div className="flex flex-col gap-2 pl-2">
            <button
              disabled={memberBusy}
              onClick={() =>
                runMemberAction(() => transferHost(roomId!, acting.playerId), '转让房主失败')
              }
              className="cut-corner py-2 border border-ink/35 text-ink text-[12px] font-semibold bg-white/25 disabled:opacity-50 active:scale-[0.97]"
            >
              把房主交给他
            </button>
            <button
              disabled={memberBusy}
              onClick={() =>
                runMemberAction(() => kickPlayer(roomId!, acting.playerId), '移出玩家失败')
              }
              className="cut-corner py-2 bg-rust-dark text-book text-[12px] font-semibold disabled:opacity-50 active:scale-[0.97]"
            >
              移出房间
            </button>
            <button
              disabled={memberBusy}
              onClick={() => setActing(null)}
              className="cut-corner py-2 border border-ink/25 text-ink-soft text-[12px] disabled:opacity-50 active:scale-[0.97]"
            >
              取消
            </button>
          </div>
        </div>
      )}
      {error && <p className="relative z-10 text-center text-[11.5px] text-rust mb-3">{error}</p>}

      {/* 到场登记表：跟准备页的登记表同一张纸，只是登记的是"到没到、就没就绪" */}
      <div className="theme-paper paper-grain relative z-10 bg-dossier text-ink shadow-[0_1px_0_rgba(0,0,0,.34),0_10px_16px_-8px_rgba(0,0,0,.6)]">
        <div className="typed flex items-center px-3 pt-2 pb-1.5 border-b-[1.5px] border-ink/40 text-[10.5px] text-ink-soft">
          <span className="flex-1">到场登记表</span>
          <span className="font-mono">{roomCode || '------'}</span>
        </div>

        {players.length === 0 && (
          <div className="text-center py-6 text-[11.5px] text-ink-soft">正在获取房间成员…</div>
        )}

        {players.map((p) => {
          const isSelf = p.playerId === playerId
          // 房主能对别人动手；对自己和 AI 不行（踢自己会让房间永远没有房主，
          // 把房主转给 AI 等于同一件事——后端两条都挡，这里只是不给入口）。
          const manageable = isHost && !isSelf && !p.isAi
          return (
            <div
              key={p.playerId}
              onClick={() => manageable && setActing({ playerId: p.playerId, nickname: p.nickname })}
              className={`flex items-center gap-2.5 px-3 py-2.5 border-b border-ink/20 last:border-b-0 ${
                manageable ? 'cursor-pointer active:bg-ink/[0.06]' : ''
              }`}
            >
              <div
                className={`w-[34px] h-[34px] flex-none flex items-center justify-center text-[15px] bg-ink/[0.08] border ${
                  p.ready ? 'border-ink/35' : 'border-dashed border-ink/35 text-ink-soft'
                }`}
              >
                {p.isAi ? <Bot className="w-[18px] h-[18px] text-ink-soft" strokeWidth={2} /> : p.ready ? '🔍' : '○'}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[13.5px] font-bold text-ink truncate">
                  {p.nickname}
                  {isSelf && '（你）'}
                </div>
                {/* 玩家有权知道桌上哪个是补位的 AI，这不是该藏起来的信息 */}
                <div className="typed text-[10.5px] text-ink-soft">
                  {p.isAi ? 'AI 队友' : p.isHost ? '房主' : '玩家'}
                </div>
              </div>
              {/* 状态用盖章，跟准备页的「已备案 / 待填」同一种表达 */}
              <span
                className={`stamped typed text-[10px] font-bold px-1.5 py-0.5 ${
                  p.ready ? 'text-[#3d6b2f]' : 'text-[#8a6a2e] border-dashed'
                }`}
              >
                {p.ready ? '已就绪' : '未就绪'}
              </span>
            </div>
          )
        })}

        {Array.from({ length: emptySeats }).map((_, i) => (
          <div
            key={`empty-${i}`}
            className="flex items-center gap-2.5 px-3 py-2.5 border-b border-ink/20 last:border-b-0"
          >
            <div className="w-[34px] h-[34px] flex-none flex items-center justify-center border border-dashed border-ink/25 text-ink-faint text-[15px]">
              ?
            </div>
            <span className="flex-1 text-[11.5px] text-ink-soft">等待玩家加入…</span>
            {/* 空位本身就是"人不齐"的位置，补位入口放在这里最好找 */}
            {isHost && (
              <button
                onClick={handleAddAiPlayer}
                disabled={addingAi}
                className={`cut-corner flex items-center gap-1 text-[11px] font-semibold px-2.5 py-1 border transition-all ${
                  addingAi
                    ? 'border-ink/25 text-ink-soft cursor-not-allowed'
                    : 'border-brass-dark text-brass-dark bg-white/25 active:bg-brass-dark active:text-dossier active:scale-[0.96]'
                }`}
              >
                <Plus className="w-3 h-3" strokeWidth={3} />
                {addingAi ? '加入中…' : '加 AI 队友'}
              </button>
            )}
          </div>
        ))}
      </div>

      <div className="flex-1" />

      <p className="text-center text-[11.5px] text-text-body leading-relaxed mt-6 mb-3 relative z-10">
        {isHost
          ? (allReady ? '全员已就绪，点击开始游戏' : '等待所有玩家标记为已就绪')
          : (info?.storyStarted ? '房主已开始，即将进入…' : '等待房主开始游戏')}
      </p>

      {isHost ? (
        <button
          onClick={handleStartStory}
          disabled={!allReady || starting}
          className={`relative z-10 w-full py-3.5 text-[14.5px] font-bold tracking-[0.22em] indent-[0.22em] flex items-center justify-center gap-2 transition-all ${
            allReady && !starting
              ? 'seal bg-brass-dark border border-brass text-text-primary active:translate-y-[1px]'
              : 'bg-panel border border-border-mid text-text-dim cursor-not-allowed'
          }`}
        >
          {starting ? '开始中…' : '开始游戏'}
        </button>
      ) : (
        <button
          onClick={toggleReady}
          className="cut-corner relative z-10 w-full py-3.5 border border-brass-dark bg-input text-brass-bright text-[14.5px] font-bold tracking-[0.14em] indent-[0.14em] active:bg-panel transition-all flex items-center justify-center gap-2"
        >
          <UserPlus className="w-4 h-4" />
          {ready ? '取消就绪' : '标记为已就绪'}
        </button>
      )}
      {startError && (
        <p className="relative z-10 text-center text-[11.5px] text-rust mt-2">{startError}</p>
      )}
    </div>
  )
}
