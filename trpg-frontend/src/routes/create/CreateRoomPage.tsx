import { useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { Plus, Minus } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import { GAME_REGISTRY, SYSTEM_NAMES, getScenarioById } from '@/config/games'
import { useGameStore } from '@/stores/game-store'
import { useAuthStore } from '@/stores/auth-store'
import { DEFAULT_MAX_PLAYERS, useRoomStore } from '@/stores/room-store'
import { createGameRoom, listModules, selectModule } from '@/services/room'
import { friendlyErrorMessage } from '@/services/api-client'

const MIN_PLAYERS = 1
// 后端 RoomCreate.max_players 的校验是 le=20（trpg-backend/app/dto/room.py），
// 这里的加减号/输入框都要跟着限制到 20，否则提交时只会收到一个 422（见
// PR #67 review）。
const MAX_PLAYERS = 20

export default function CreateRoomPage() {
  const navigate = useNavigate()
  const store = useGameStore()
  const nickname = useAuthStore((s) => s.nickname)
  const setRoomIdentity = useRoomStore((s) => s.setRoomIdentity)
  const setStoreModuleId = useRoomStore((s) => s.setModuleId)
  const setCreateForm = useRoomStore((s) => s.setCreateForm)
  const setHost = useRoomStore((s) => s.setHost)
  const savedRoomName = useRoomStore((s) => s.createFormRoomName)
  const savedMaxPlayers = useRoomStore((s) => s.createFormMaxPlayers)
  const [roomName, setRoomName] = useState(savedRoomName || '')
  // 默认 1 人：零基础玩家的第一局多半是自己先开一间试试，默认 4 会让他对着
  // 三个空位等人（真人实测反馈 exec/23 #50）。要多人自己往上加。
  // 默认值只有 room-store 那一个来源，这里不再写第二份字面量。
  const [maxPlayers, setMaxPlayers] = useState(savedMaxPlayers || DEFAULT_MAX_PLAYERS)
  const [maxPlayersInput, setMaxPlayersInput] = useState(
    String(savedMaxPlayers || DEFAULT_MAX_PLAYERS)
  )
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')

  const selectedGame = store.gameId ? GAME_REGISTRY.find(g => g.id === store.gameId) : null
  const systemName = store.systemId ? SYSTEM_NAMES[store.systemId] : null
  const selectedScenario = store.sceneId ? getScenarioById(store.sceneId) : null
  const hasSelection = !!(store.gameId && store.systemId && store.sceneId)

  const handleCreate = async () => {
    if (!roomName.trim() || !hasSelection) return
    setCreating(true)
    setCreateError('')
    try {
      const room = await createGameRoom(nickname || undefined, roomName.trim(), maxPlayers)
      // 必须先把房间身份（含 reconnectToken）写进 store，selectModule 等
      // 需要重连凭证的接口才能读到它——见 issue #66，真机联调时发现的顺序 bug。
      setRoomIdentity(room)
      // sceneId 与后端 catalog scenario_id 对齐（见 config/games.ts）
      const moduleId = store.sceneId
      if (!moduleId) throw new Error('请先选择模组')
      const modules = await listModules()
      if (modules.length === 0) throw new Error('暂无可用模组')
      const hit = modules.find((m) => m.id === moduleId)
      if (!hit) {
        throw new Error(
          '所选模组在服务端不可用（仅支持已 seed 的可玩模组，如「追书人」「科比特先生」）'
        )
      }
      await selectModule(room.roomId, hit.id)
      setStoreModuleId(hit.id)
      setHost(true)
      navigate('/room/lobby')
    } catch (err) {
      setCreateError(friendlyErrorMessage(err, '创建房间失败'))
    } finally {
      setCreating(false)
    }
  }

  const canCreate = roomName.trim().length > 0 && hasSelection && !creating

  const handleSelectGame = () => {
    setCreateForm({ roomName, maxPlayers })
    store.reset()
    store.setReturnFromGameSelect(true)
    navigate('/home/create/games')
  }

  const handleChangeGame = () => {
    setCreateForm({ roomName, maxPlayers })
    store.reset()
    store.setReturnFromGameSelect(true)
    navigate('/home/create/games')
  }

  const sectionLabel =
    'inline-block text-[10.5px] font-bold tracking-[0.16em] bg-text-primary text-page px-2 py-[3px] mb-3'

  return (
    <ShellPage
      title="创建房间"
      onBack={() => { store.reset(); setCreateForm({ roomName: '', maxPlayers: DEFAULT_MAX_PLAYERS }); navigate('/home') }}
    >
      <div className="px-5 flex flex-col gap-3">
        {/* ── Room Settings ── */}
        <div className="press-soft bg-card p-3.5">
          <span className={sectionLabel}>房间设置</span>
          <div className="flex flex-col gap-3">
            <div>
              <label className="text-[10.5px] font-bold text-text-muted mb-1.5 block">房间名称</label>
              <input value={roomName} onChange={e => setRoomName(e.target.value)}
                placeholder="例如：阿卡姆调查团" className="shell-field w-full px-3 py-2 text-[14px]" />
            </div>
            <div>
              <label className="text-[10.5px] font-bold text-text-muted mb-1.5 block">最大人数</label>
              <div className="flex items-center gap-3">
                <button
                  onClick={() => {
                    const next = Math.max(MIN_PLAYERS, maxPlayers - 1)
                    setMaxPlayers(next)
                    setMaxPlayersInput(String(next))
                  }}
                  disabled={maxPlayers <= MIN_PLAYERS}
                  className="press w-9 h-9 bg-page text-text-primary flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed">
                  <Minus className="w-[16px] h-[16px]" />
                </button>
                <div className="flex-1 flex items-center justify-center gap-1">
                  <input
                    type="number"
                    inputMode="numeric"
                    min={MIN_PLAYERS}
                    max={MAX_PLAYERS}
                    value={maxPlayersInput}
                    onChange={e => setMaxPlayersInput(e.target.value)}
                    onBlur={() => {
                      const v = parseInt(maxPlayersInput, 10)
                      const clamped = Number.isNaN(v)
                        ? maxPlayers
                        : Math.min(MAX_PLAYERS, Math.max(MIN_PLAYERS, v))
                      setMaxPlayers(clamped)
                      setMaxPlayersInput(String(clamped))
                    }}
                    className="w-16 text-center text-[18px] font-extrabold font-mono text-text-primary bg-transparent outline-none [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
                  />
                  <span className="text-[13px] text-text-muted">人</span>
                </div>
                <button
                  onClick={() => {
                    const next = Math.min(MAX_PLAYERS, maxPlayers + 1)
                    setMaxPlayers(next)
                    setMaxPlayersInput(String(next))
                  }}
                  disabled={maxPlayers >= MAX_PLAYERS}
                  className="press w-9 h-9 bg-page text-text-primary flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed">
                  <Plus className="w-[16px] h-[16px]" />
                </button>
              </div>
              <p className="text-[10.5px] text-text-dim mt-1.5">最多 {MAX_PLAYERS} 人</p>
            </div>
          </div>
        </div>

        {/* ── Select Game ── */}
        <div className="press-soft bg-card p-3.5">
          <span className={sectionLabel}>选择游戏</span>

          {hasSelection ? (
            <div className="flex items-center gap-3 px-3 py-2.5 border-2 border-rust bg-page">
              <div className="w-10 h-10 border-2 border-text-primary bg-ink-blue text-[#fff5ea] flex items-center justify-center text-[15px] font-extrabold">
                {selectedScenario?.nameEn?.charAt(0) || '🎮'}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[13px] font-bold text-text-primary">{selectedGame?.name} · {systemName}</div>
                <div className="text-[11px] text-text-muted mt-0.5">模组：{selectedScenario?.name}</div>
              </div>
              <button onClick={handleChangeGame}
                className="text-[11px] font-bold text-text-primary underline whitespace-nowrap">更换</button>
            </div>
          ) : (
            <div className="text-center">
              <p className="text-[11.5px] text-text-muted mb-2.5">选择一个游戏、规则和模组</p>
              <button onClick={handleSelectGame}
                className="w-full py-2.5 border-2 border-dashed border-text-primary/45 text-text-body text-[13px] font-semibold active:bg-page transition-all flex items-center justify-center gap-2">
                <Plus className="w-[18px] h-[18px]" />
                选择游戏
              </button>
            </div>
          )}
        </div>

        {/* ── Room Summary ── */}
        <div className="press-soft bg-card p-3.5">
          <span className={sectionLabel}>房间概览</span>
          <div className="flex flex-col gap-1.5 text-[13px] text-text-body">
            {[
              { k: '房间名', v: roomName || '未设置', strong: true },
              { k: '游戏', v: selectedGame?.name || (store.gameId || '未选择') },
              { k: '规则', v: systemName || (store.systemId || '未选择') },
              { k: '模组', v: selectedScenario?.name || '未选择' },
              { k: '人数上限', v: `${maxPlayers} 人` },
            ].map((row) => (
              <div key={row.k} className="flex items-center justify-between border-b border-dotted border-text-primary/25 pb-1">
                <span className="text-[11.5px] text-text-muted">{row.k}</span>
                <span className={`text-text-primary ${row.strong ? 'font-bold' : ''}`}>{row.v}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* 🔴 按钮**紧跟着最后一张卡**，既不 `fixed` 也不 `mt-auto` 贴底。
          三种做法各错一次，值得留着：
            `fixed` + 内容区 `pb-24` → 两段留白叠起来，底部空约 166px；
            `mt-auto`               → 按钮被顶到屏幕最底，概览与它之间空一大块。
          这页是**从上往下填的表单**，最后一步就该接在最后一栏后面。页面底部
          剩下的空白是纸板本身，不需要被内容填满。 */}
      <div className="px-5 pt-3">
        {createError && <p className="text-[11.5px] text-rust-dark text-center mb-2">{createError}</p>}
        <button onClick={handleCreate} disabled={!canCreate}
          className={`w-full py-3 text-[14px] font-extrabold tracking-[0.16em] indent-[0.16em] transition-all flex items-center justify-center gap-2 ${
            canCreate
              ? 'press bg-rust text-[#fff5ea]'
              : 'border-2 border-dashed border-text-primary/35 text-text-muted cursor-not-allowed'
          }`}>
          <Plus className="w-[18px] h-[18px]" /> {creating ? '创建中…' : '创建房间'}
        </button>
      </div>
    </ShellPage>
  )
}
