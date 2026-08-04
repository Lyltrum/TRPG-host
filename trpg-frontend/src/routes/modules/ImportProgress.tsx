import { Check, X } from 'lucide-react'
import {
  STAGE_LABELS,
  STAGE_ORDER,
  stageProgress,
  type ModuleImportJob,
} from '@/services/module-import'

/**
 * 转换进度：当前阶段一句话 + 进度条 + 六步清单。
 *
 * 🔴 **清单和进度条各管一件事，缺一不可。**清单只说"在第几件事"，进度条才说
 * "还剩多少"——而这一屏用户要盯十几分钟。反过来，进度条丢掉了"在哪一步断的"，
 * 而那正是失败时用户唯一需要的信息（"读取文稿"失败＝换个文件；"组装"失败＝
 * 这份模组转不了）。所以**同一份清单在失败时变成证据**：出问题那一格打叉。
 *
 * `compact` 只画标题行 + 进度条，用在列表卡片里。
 */
export default function ImportProgress({
  job,
  compact = false,
}: {
  job: ModuleImportJob
  compact?: boolean
}) {
  const failed = job.status === 'failed' || job.status === 'interrupted'
  const currentIndex = STAGE_ORDER.indexOf(
    (job.stage ?? '') as (typeof STAGE_ORDER)[number]
  )
  const percent = stageProgress(job.stage)
  const currentLabel = STAGE_LABELS[job.stage ?? ''] ?? '准备中'

  return (
    <div className={compact ? 'mt-2' : ''}>
      <div className="flex items-baseline justify-between gap-2">
        <span
          className={`font-extrabold ${compact ? 'text-[12px]' : 'text-[17px]'} ${
            failed ? 'text-rust-dark' : 'text-text-primary'
          }`}
        >
          {failed ? `停在「${currentLabel}」` : `正在${currentLabel}`}
        </span>
        <span className="text-[11.5px] text-text-muted font-mono flex-shrink-0">
          {/* 未知阶段时 currentIndex 是 -1，显示 0 而不是把它当成第 0 步 */}
          {Math.max(0, currentIndex + 1)} / {STAGE_ORDER.length}
        </span>
      </div>

      <div className="mt-2 h-[14px] border-2 border-text-primary bg-card relative overflow-hidden">
        <div
          className={`absolute inset-y-0 left-0 ${failed ? 'bg-rust-dark' : 'bg-rust'}`}
          style={{ width: `${percent}%` }}
        />
        <div
          className="absolute inset-0"
          style={{
            backgroundImage:
              'repeating-linear-gradient(-45deg, rgba(36,31,25,.22) 0 4px, transparent 4px 9px)',
          }}
        />
      </div>

      {!compact && (
        <div className="mt-4">
          {STAGE_ORDER.map((stage, i) => {
            const done = i < currentIndex
            const now = i === currentIndex
            return (
              <div
                key={stage}
                className="flex items-center gap-2.5 py-2 border-b-[1.5px] border-text-primary/15 last:border-b-0"
              >
                <StepDot index={i} done={done} now={now} failed={failed && now} />
                <span
                  className={`text-[13px] ${
                    failed && now
                      ? 'font-bold text-rust-dark'
                      : done || now
                        ? 'font-bold text-text-primary'
                        : 'font-semibold text-text-dim'
                  }`}
                >
                  {STAGE_LABELS[stage]}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function StepDot({
  index,
  done,
  now,
  failed,
}: {
  index: number
  done: boolean
  now: boolean
  failed: boolean
}) {
  const base =
    'w-5 h-5 flex-shrink-0 border-2 flex items-center justify-center text-[10.5px] font-extrabold'
  if (failed) {
    return (
      <span className={`${base} bg-rust-dark border-text-primary text-[#fff5ea]`}>
        <X className="w-3 h-3" strokeWidth={3} />
      </span>
    )
  }
  if (done) {
    return (
      <span className={`${base} bg-mold border-mold text-white`}>
        <Check className="w-3 h-3" strokeWidth={3} />
      </span>
    )
  }
  if (now) {
    return <span className={`${base} bg-rust border-text-primary text-[#fff5ea]`}>◐</span>
  }
  return (
    <span className={`${base} border-text-primary/30 text-text-dim bg-transparent`}>
      {index + 1}
    </span>
  )
}
