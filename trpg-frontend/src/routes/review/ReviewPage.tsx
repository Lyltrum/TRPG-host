import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Copy, ScrollText } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import { getRoomInfo, getRoomSummary, type RoomPreview, type RoomSummary } from '@/services/room'
import { friendlyErrorMessage } from '@/services/api-client'
import { buildReviewText, copyText } from './review-text'

export default function ReviewPage() {
  const navigate = useNavigate()
  const { roomCode } = useParams<{ roomCode: string }>()
  const [room, setRoom] = useState<RoomPreview | null>(null)
  const [summary, setSummary] = useState<RoomSummary | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [copied, setCopied] = useState(false)
  const [fallbackText, setFallbackText] = useState('')

  useEffect(() => {
    if (!roomCode) return
    getRoomInfo(roomCode)
      .then(setRoom)
      .catch((err) => setError(friendlyErrorMessage(err, '加载复盘失败')))
  }, [roomCode])

  // 摘要按 roomId 取，所以要等房间信息回来。第一次打开时后端会现算一次
  // （其中那段回顾要打一次网络），之后直接读库——所以这里的等待是真的在等，
  // 不是装出来的进度条（原来那版是 setTimeout 900ms 的假动画）。
  useEffect(() => {
    if (!room) return
    let alive = true
    getRoomSummary(room.roomId)
      .then((data) => alive && setSummary(data))
      .catch((err) => alive && setError(friendlyErrorMessage(err, '加载复盘摘要失败')))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [room])

  /**
   * 复制整段复盘，供玩家发到群里。
   *
   * 🔴 **必须有降级路**：`navigator.clipboard` 在**非安全上下文里不存在**，
   * 而这个项目的主场恰恰是局域网 `http://<内网IP>:9877`。复制不成就把文本
   * 摊开让用户自己长按选中——**不假装复制成功**。
   */
  const handleCopy = async () => {
    if (!room) return
    const text = buildReviewText(room, summary)
    if (await copyText(text)) {
      setCopied(true)
      setFallbackText('')
      window.setTimeout(() => setCopied(false), 2000)
    } else {
      setFallbackText(text)
    }
  }

  return (
    <ShellPage title="复盘" onBack={() => navigate('/home/my-rooms')}>
      <div className="px-5 flex flex-col gap-3.5">
        {error && <p className="text-[11.5px] text-rust-dark text-center">{error}</p>}

        {room && (
          <>
            <div className="press-soft bg-card p-3.5">
              <div className="text-[15px] font-extrabold text-text-primary">{room.roomName}</div>
              <div className="text-[11.5px] text-text-muted mt-0.5">{room.moduleTitle || '未知模组'} · 已完成</div>
            </div>

            {/* 数字那一半：代码算的，一定有。放在回顾前面——它是确定的。 */}
            {summary && summary.highlights && summary.highlights.length > 0 && (
              <div className="press-soft bg-card p-3.5">
                <span className="inline-block text-[10.5px] font-bold tracking-[0.14em] bg-text-primary text-page px-2 py-[3px] mb-2.5">
                  这一局
                </span>
                <div className="flex flex-col gap-1.5">
                  {summary.highlights.map((line, i) => (
                    <div key={i} className="text-[13px] text-text-body leading-[1.7]">
                      · {line}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 🔴 没查到的真相（2026-08-19）。真人 KP 收场时一定会把谜底讲出来，
                那是玩家最在乎的部分；而 `kpTruth` 此前只进裁决 prompt，没有任何
                通往玩家的出口。玩家可以主动收工（内容没跑完就结束）之后，拿不到
                交代会变成默认结果。

                后端守着门：只有对局真的 `finished` 才非空。`null` = 还没结束，
                `[]` = 全查到了——两者含义不同，所以判的是长度而不是真值。 */}
            {summary?.missedTruths && summary.missedTruths.length > 0 && (
              <div className="press-soft bg-card p-3.5">
                <span className="inline-block text-[10.5px] font-bold tracking-[0.14em] bg-text-primary text-page px-2 py-[3px] mb-2.5">
                  你们没查到的
                </span>
                <div className="flex flex-col gap-2">
                  {summary.missedTruths.map((line, i) => (
                    <div key={i} className="text-[13px] text-text-body leading-[1.75]">
                      · {line}
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="press-soft bg-card p-3.5">
              <span className="inline-flex items-center gap-1.5 text-[10.5px] font-bold tracking-[0.14em] bg-text-primary text-page px-2 py-[3px] mb-2.5">
                <ScrollText className="w-[13px] h-[13px]" /> 案件回顾
              </span>
              {loading ? (
                <p className="text-[13px] text-text-dim py-4 text-center animate-pulse">复盘摘要生成中…</p>
              ) : summary?.summaryText ? (
                <p className="text-[13px] text-text-body leading-[1.85]">{summary.summaryText}</p>
              ) : (
                // 🔴 没配 DeepSeek key 时后端如实返回 null。**照实说**——原来这里
                // 是一段假的占位文案，读起来像"生成好了"，而其实什么都没有。
                <p className="text-[13px] text-text-dim leading-[1.85]">
                  这一局没有生成文字回顾（守秘人没接上大模型）。上面的数字是完整的。
                </p>
              )}
            </div>

            {/* 复盘的出口（`exec/46` B9）。摘要还在生成时不给按——那时复制
                出去的是半份。 */}
            <button
              onClick={handleCopy}
              disabled={loading}
              className="press w-full py-3 text-[13.5px] font-bold border-2 border-text-primary/45 text-text-muted flex items-center justify-center gap-2 active:bg-card disabled:opacity-40 transition-all"
            >
              <Copy className="w-[16px] h-[16px]" /> {copied ? '已复制' : '复制这份复盘'}
            </button>

            {fallbackText && (
              <div className="press-soft bg-card p-3.5 flex flex-col gap-2">
                <p className="text-[11px] text-text-muted leading-relaxed">
                  这个浏览器不让网页直接写剪贴板（局域网 http 打开时常见）。
                  <span className="font-bold text-text-primary">长按下面的文字全选复制</span>。
                </p>
                <textarea
                  readOnly
                  value={fallbackText}
                  onFocus={(e) => e.currentTarget.select()}
                  rows={10}
                  className="shell-field w-full px-3 py-2 text-[12px] leading-[1.7] font-mono"
                />
              </div>
            )}

            <div className="press-soft bg-card p-3.5">
              <span className="inline-block text-[10.5px] font-bold tracking-[0.14em] bg-text-primary text-page px-2 py-[3px] mb-2.5">
                参与调查员
              </span>
              <div className="flex flex-col gap-1.5">
                {room.players.map((p) => (
                  <div key={p.playerId} className="flex items-center gap-2.5 px-2.5 py-1.5 border-2 border-text-primary/25">
                    <div className="w-8 h-8 border-2 border-text-primary bg-page flex items-center justify-center text-[14px] flex-shrink-0">🔍</div>
                    <div className="flex-1 min-w-0">
                      <div className="text-[13px] font-bold text-text-primary">{p.nickname}</div>
                      <div className="text-[10.5px] text-text-muted">{p.isHost ? '房主' : '玩家'}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </ShellPage>
  )
}
