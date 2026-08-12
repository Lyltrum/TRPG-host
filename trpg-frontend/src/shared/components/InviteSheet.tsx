import { useEffect, useState } from 'react'
import { Check, Copy, X } from 'lucide-react'
import QRCode from 'qrcode'

/** 邀请链接。**只带房间码**，域名用当前 host——局域网 IP 换了、以后上公网了，
 *  这段代码都不用改（同族于「路径别数层数，找锚点」）。 */
export function inviteUrlFor(roomCode: string): string {
  return `${window.location.origin}/join/${roomCode}`
}

/**
 * 邀请弹层：链接 + 复制 + 二维码。
 *
 * 🔴 二维码是**同桌扫**的场景，链接是**发微信**的场景，两个都要——聚会时
 * 两种都会发生。房间码本身仍然留在大厅上（有人就是想手输）。
 */
export default function InviteSheet({ roomCode, onClose }: { roomCode: string; onClose: () => void }) {
  const url = inviteUrlFor(roomCode)
  const [qr, setQr] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let alive = true
    // 二维码在本地画，不请求任何外部服务——发布出去的页面有严格 CSP，
    // 而且房间码不该被送到第三方。
    QRCode.toDataURL(url, { margin: 1, width: 320, errorCorrectionLevel: 'M' })
      .then((data) => {
        if (alive) setQr(data)
      })
      .catch(() => {
        if (alive) setQr(null)
      })
    return () => {
      alive = false
    }
  }, [url])

  const handleCopy = async () => {
    try {
      // navigator.clipboard 只在安全上下文可用（HTTPS / localhost）。朋友走
      // http://<局域网IP> 打开时它是 undefined —— 退回一次性 textarea + execCommand，
      // 否则复制按钮在最需要它的那个场景里正好是坏的。
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(url)
      } else {
        const ta = document.createElement('textarea')
        ta.value = url
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
      }
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/55" onClick={onClose}>
      <div
        className="theme-paper paper-grain relative w-full max-w-[420px] bg-dossier text-ink px-4 pt-4 pb-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="typed flex items-center border-b-[1.5px] border-ink/40 pb-2 mb-3 text-[11px] text-ink-soft">
          <span className="flex-1">邀请调查员</span>
          <button type="button" onClick={onClose} aria-label="关闭">
            <X className="w-4 h-4" strokeWidth={2} />
          </button>
        </div>

        {qr && (
          <div className="flex justify-center mb-3">
            <img src={qr} alt={`房间 ${roomCode} 的二维码`} className="w-[176px] h-[176px] bg-white p-1.5" />
          </div>
        )}

        <p className="text-center text-[11.5px] text-ink-soft mb-1">让朋友扫码，或把链接发给他</p>

        <div className="flex items-center gap-2 mt-3">
          <span className="flex-1 min-w-0 truncate font-mono text-[11.5px] bg-ink/[0.07] border border-ink/25 px-2.5 py-2">
            {url}
          </span>
          <button
            type="button"
            onClick={handleCopy}
            className="cut-corner flex-none flex items-center gap-1 px-3 py-2 bg-brass-dark text-book text-[12px] font-semibold active:scale-[0.97]"
          >
            {copied ? <Check className="w-3.5 h-3.5" strokeWidth={2.5} /> : <Copy className="w-3.5 h-3.5" strokeWidth={2} />}
            {copied ? '已复制' : '复制'}
          </button>
        </div>

        <p className="text-center text-[11px] text-ink-soft mt-3">
          也可以直接报房间号 <span className="font-mono font-bold tracking-[0.18em]">{roomCode}</span>
        </p>
      </div>
    </div>
  )
}
