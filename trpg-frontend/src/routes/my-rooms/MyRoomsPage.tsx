import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Play, ScrollText, Hash, Plus, Trash2 } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import {
  listMyRooms,
  joinRoomByCode,
  deleteRoom,
  type MyRoomSummary,
} from '@/services/room'
import { friendlyErrorMessage } from '@/services/api-client'
import { useAuthStore } from '@/stores/auth-store'
import { useRoomStore } from '@/stores/room-store'

const PHASE_LABEL: Record<string, string> = {
  Lobby: '大厅等待中',
  InGame: '游戏进行中',
  Completed: '已完成',
}

// 后端 MyRoomSummary.updatedAt 是 ISO-8601 字符串（pydantic 的 datetime 序列化
// 结果），不是数字时间戳。这里必须先 Date.parse 成毫秒再参与算术——直接拿字符串
// 去减会得到 NaN，最终一路落到 new Date(NaN) 显示成 "Invalid Date"（issue #75
// 引入 codegen 时发现的真实 bug：此前 SDK 手写类型误标成 number，与这里的错误
// 用法互相掩盖，TS 一直没能报出来）。
function formatTime(ts: string): string {
  const parsed = Date.parse(ts)
  if (Number.isNaN(parsed)) return '未知时间'
  const diffMin = Math.round((Date.now() - parsed) / 60000)
  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin} 分钟前`
  const diffHour = Math.round(diffMin / 60)
  if (diffHour < 24) return `${diffHour} 小时前`
  return new Date(parsed).toLocaleDateString('zh-CN')
}

// 房间阶段 → 重新进入时该落到哪个页面。
//
// 不含 `Completed`：已完成的房间在下面是单独一个列表、走「查看复盘」按钮
// （`/home/my-rooms/review/:roomCode`），根本不会调到 `handleResume`。放一条
// 到不了的映射进来只会误导人——而且一旦写错路由，谁也发现不了。
const RESUME_ROUTE: Record<string, string> = {
  Lobby: '/room/lobby',
  Building: '/room/ready',
  InGame: '/room/play',
}

/**
 * 删除确认条。**代价要写在按下之前**：删房间连这一局的复盘一起没，而复盘是
 * 玩完之后唯一还看得到的东西——只写「确定删除吗」等于没提示。
 */
function DeleteConfirm({
  deleting,
  onConfirm,
  onCancel,
}: {
  deleting: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <div className="bg-page p-2.5 flex flex-col gap-2">
      <p className="text-[11px] text-text-muted leading-relaxed">
        删了就找不回来了——这一局的记录和<span className="font-bold text-text-primary">复盘也会一起没</span>。
      </p>
      <div className="flex items-center gap-1.5">
        <button
          onClick={onConfirm}
          disabled={deleting}
          className="press px-2.5 py-1.5 text-[11.5px] font-bold bg-rust text-[#fff5ea] disabled:opacity-60 whitespace-nowrap"
        >
          {deleting ? '删除中…' : '确认删除'}
        </button>
        <button
          onClick={onCancel}
          className="press px-2.5 py-1.5 text-[11.5px] font-bold bg-card text-text-muted whitespace-nowrap"
        >
          取消
        </button>
      </div>
    </div>
  )
}

export default function MyRoomsPage() {
  const navigate = useNavigate()
  const nickname = useAuthStore((s) => s.nickname)
  const setRoomIdentity = useRoomStore((s) => s.setRoomIdentity)
  const [rooms, setRooms] = useState<MyRoomSummary[] | null>(null)
  const [error, setError] = useState('')
  const [resumingCode, setResumingCode] = useState<string | null>(null)
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  useEffect(() => {
    listMyRooms()
      .then(setRooms)
      .catch((err) => setError(friendlyErrorMessage(err, '加载房间列表失败')))
  }, [])

  const inProgress = rooms?.filter((r) => r.phase !== 'Completed') ?? []
  const completed = rooms?.filter((r) => r.phase === 'Completed') ?? []

  /**
   * 删房间是**不可撤回**的（房间、事件流、角色卡、复盘一起没），所以：
   * ①只有房主能看见那个键；②必须点两下，第二下前把代价写在脸上。
   */
  const handleDelete = async (room: MyRoomSummary) => {
    setDeletingId(room.roomId)
    setError('')
    try {
      await deleteRoom(room.roomId)
      setRooms((prev) => (prev ?? []).filter((r) => r.roomId !== room.roomId))
    } catch (err) {
      setError(friendlyErrorMessage(err, '删除房间失败'))
    } finally {
      setDeletingId(null)
      setConfirmingId(null)
    }
  }

  const handleResume = async (room: MyRoomSummary) => {
    setResumingCode(room.roomCode)
    setError('')
    try {
      // 房主身份直接从 join 的返回里拿。原来是再拉一次房间预览、从玩家列表里
      // 找自己 —— 多一次请求，而且 `?? false` 意味着预览里没找到自己时会**静默**
      // 把真房主降成访客。
      setRoomIdentity(await joinRoomByCode(room.roomCode, nickname || undefined))
      // 按房间阶段回到对应的页面。原来只区分 InGame / 其它，Building 阶段
      // （已经过了大厅、正在建卡）会被送回大厅——而大厅的"开始游戏"在这个阶段
      // 必然被后端 409 拒绝，用户就卡在那儿了。房间状态机是
      // Lobby → Building → InGame → Completed，这里要一一对上。
      navigate(RESUME_ROUTE[room.phase] ?? '/room/lobby')
    } catch (err) {
      setError(friendlyErrorMessage(err, '继续游戏失败'))
    } finally {
      setResumingCode(null)
    }
  }

  return (
    <ShellPage title="我的游戏" onBack={() => navigate('/home')} align="top">
      <div className="px-5 flex flex-col gap-5">
        {error && <p className="text-[11.5px] text-rust-dark text-center">{error}</p>}

        {rooms === null && !error && (
          <p className="text-center text-[13px] text-text-dim py-10">加载中…</p>
        )}

        {rooms !== null && rooms.length === 0 && (
          <div className="text-center py-14 flex flex-col gap-4">
            <p className="text-[13px] text-text-muted">还没有加入过任何房间</p>
            <div className="flex flex-col gap-2.5 px-6">
              <button onClick={() => navigate('/home/create')}
                className="press w-full py-3 flex items-center justify-center gap-2 text-[13.5px] font-extrabold tracking-[0.12em] bg-ink-blue text-[#fff5ea]">
                <Plus className="w-[16px] h-[16px]" /> 创建房间
              </button>
              <button onClick={() => navigate('/home/join')}
                className="press w-full py-3 flex items-center justify-center gap-2 text-[13.5px] font-extrabold tracking-[0.12em] bg-card text-text-primary">
                <Hash className="w-[16px] h-[16px]" /> 加入房间
              </button>
            </div>
          </div>
        )}

        {/* 分组标题做成**贴在纸板上的标签**：实心墨块反白，跟卡片拉开层级 */}
        {inProgress.length > 0 && (
          <div>
            <span className="inline-block text-[10.5px] font-bold tracking-[0.16em] bg-text-primary text-page px-2 py-[3px] mb-2.5">
              进行中
            </span>
            <div className="flex flex-col gap-2.5">
              {inProgress.map((room) => (
                <div key={room.roomCode} className="press-soft bg-card p-3 flex flex-col gap-2">
                  <div className="flex items-center gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="text-[13.5px] font-bold text-text-primary truncate">{room.roomName}</div>
                      <div className="text-[10.5px] text-text-muted mt-0.5">
                        {room.moduleTitle || '尚未选择模组'} · {PHASE_LABEL[room.phase] || room.phase} · {formatTime(room.updatedAt)}
                      </div>
                    </div>
                    <button
                      onClick={() => handleResume(room)}
                      disabled={resumingCode === room.roomCode}
                      className="press flex items-center gap-1 px-3 py-1.5 text-[11.5px] font-bold bg-rust text-[#fff5ea] disabled:opacity-60 whitespace-nowrap"
                    >
                      <Play className="w-[14px] h-[14px]" />
                      {resumingCode === room.roomCode ? '进入中…' : '继续'}
                    </button>
                    {room.isHost && confirmingId !== room.roomId && (
                      <button
                        onClick={() => setConfirmingId(room.roomId)}
                        aria-label={`删除 ${room.roomName}`}
                        className="press flex-none p-1.5 text-text-muted"
                      >
                        <Trash2 className="w-[15px] h-[15px]" />
                      </button>
                    )}
                  </div>
                  {confirmingId === room.roomId && (
                    <DeleteConfirm
                      deleting={deletingId === room.roomId}
                      onCancel={() => setConfirmingId(null)}
                      onConfirm={() => void handleDelete(room)}
                    />
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {completed.length > 0 && (
          <div>
            <span className="inline-block text-[10.5px] font-bold tracking-[0.16em] bg-text-primary text-page px-2 py-[3px] mb-2.5">
              已完成
            </span>
            <div className="flex flex-col gap-2.5">
              {completed.map((room) => (
                <div key={room.roomCode} className="press-soft bg-card p-3 flex flex-col gap-2">
                  <div className="flex items-center gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="text-[13.5px] font-bold text-text-primary truncate">{room.roomName}</div>
                      <div className="text-[10.5px] text-text-muted mt-0.5">
                        {room.moduleTitle || '未知模组'} · {formatTime(room.updatedAt)}
                      </div>
                    </div>
                    <button
                      onClick={() => navigate(`/home/my-rooms/review/${room.roomCode}`)}
                      className="press flex items-center gap-1 px-3 py-1.5 text-[11.5px] font-bold bg-card text-text-primary whitespace-nowrap"
                    >
                      <ScrollText className="w-[14px] h-[14px]" />
                      查看复盘
                    </button>
                    {room.isHost && confirmingId !== room.roomId && (
                      <button
                        onClick={() => setConfirmingId(room.roomId)}
                        aria-label={`删除 ${room.roomName}`}
                        className="press flex-none p-1.5 text-text-muted"
                      >
                        <Trash2 className="w-[15px] h-[15px]" />
                      </button>
                    )}
                  </div>
                  {confirmingId === room.roomId && (
                    <DeleteConfirm
                      deleting={deletingId === room.roomId}
                      onCancel={() => setConfirmingId(null)}
                      onConfirm={() => void handleDelete(room)}
                    />
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </ShellPage>
  )
}
