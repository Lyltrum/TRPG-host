import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Upload } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import Badge from '@/shared/components/Badge'
import ImportProgress from './ImportProgress'
import {
  deleteModule,
  isJobRunning,
  listModuleImports,
  type ModuleImportJob,
} from '@/services/module-import'

/** 正在转的 job 多久轮一次。转换要跑 5–26 分钟，不需要更密。 */
const POLL_MS = 4000

/**
 * 「我的模组」——两个入口的落点，也是**「转换途中关掉页面之后怎么回来」的答案**。
 *
 * 正在转的、转好的、没转成的都在这里，靠左脊色区分。有了这一屏就不需要通知或
 * 小红点了（这一版也没有推送能力，做个假入口反而更糟）。
 */
export default function MyModulesPage() {
  const navigate = useNavigate()
  const [jobs, setJobs] = useState<ModuleImportJob[] | null>(null)
  const [error, setError] = useState('')
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  // 🔴 轮询只在**有 job 在跑**时开。全都是终态还接着轮，等于白烧电池——
  // 而这一屏用户可能停留很久（他在等一件十几分钟的事）。
  const hasRunning = jobs?.some(isJobRunning) ?? false
  const hasRunningRef = useRef(hasRunning)
  hasRunningRef.current = hasRunning

  const load = useCallback(async () => {
    try {
      setJobs(await listModuleImports())
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取导入记录失败')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * 删掉一份模组。
   *
   * 🔴 **失败的那句话要原样给用户看**：后端在"有房间在用"时会连房间数和出路
   * （先解散）一起返回，那是**下一步**，换成"删除失败"就把它扔了。
   *
   * 🔴 删除中要有可见状态：这是一次网络往返，没有它用户会以为没反应
   * （2026-08-19 装备审核那次踩过）。
   */
  const remove = useCallback(
    async (job: ModuleImportJob) => {
      if (!job.resultScenarioId) return
      setDeletingId(job.jobId)
      setError('')
      try {
        await deleteModule(job.resultScenarioId)
        setConfirmingId(null)
        await load()
      } catch (err) {
        setError(err instanceof Error ? err.message : '删不掉这份模组')
      } finally {
        setDeletingId(null)
      }
    },
    [load]
  )

  useEffect(() => {
    if (!hasRunning) return
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [hasRunning, load])

  return (
    <ShellPage title="我的模组" onBack={() => navigate('/home')} align="top">
      <p className="text-[11.5px] text-text-muted px-5 pb-3.5">
        你自己导入的模组，创建房间时可以选
      </p>

      <div className="px-5 flex flex-col gap-3">
        {error && <p className="text-[12.5px] text-rust font-bold">{error}</p>}

        {jobs !== null && jobs.length === 0 && !error && (
          <p className="text-center py-8 text-text-muted text-[12.5px] leading-[1.9]">
            还没有导入过模组。
            <br />
            在网上找到的 COC 模组，传上来就能玩。
          </p>
        )}

        {jobs?.map((job) => (
          <JobCard
            key={job.jobId}
            job={job}
            onOpen={() => navigate(`/home/modules/${job.jobId}`)}
            confirming={confirmingId === job.jobId}
            deleting={deletingId === job.jobId}
            onAskDelete={() => {
              setConfirmingId(job.jobId)
              setError('')
            }}
            onCancelDelete={() => setConfirmingId(null)}
            onDelete={() => void remove(job)}
          />
        ))}
      </div>

      <div className="px-5 mt-4 mb-6">
        <button
          onClick={() => navigate('/home/modules/import')}
          className="w-full flex items-center justify-center gap-2 py-3 border-2 border-dashed border-text-primary/45 text-text-muted text-[13px] font-semibold active:bg-card transition-all"
        >
          <Upload className="w-[18px] h-[18px]" />
          导入新模组
        </button>
      </div>
    </ShellPage>
  )
}

/** 左脊色就是状态：金＝转换中、蓝＝已就绪、红＝没转成、灰＝中断。 */
const SPINE: Record<string, string> = {
  pending: 'border-l-brass-bright',
  running: 'border-l-brass-bright',
  succeeded: 'border-l-ink-blue',
  failed: 'border-l-rust',
  interrupted: 'border-l-text-muted',
}

function JobCard({
  job,
  onOpen,
  onDelete,
  confirming,
  deleting,
  onAskDelete,
  onCancelDelete,
}: {
  job: ModuleImportJob
  onOpen: () => void
  onDelete: () => void
  confirming: boolean
  deleting: boolean
  onAskDelete: () => void
  onCancelDelete: () => void
}) {
  const spine = SPINE[job.status] ?? SPINE.interrupted
  // 🔴 只有真的产出了一份模组才谈得上删。失败/中断的 job 是**历史记录**，
  // 它的失败理由要留着（用户点三次重试得知道前两次为什么failed）。
  const removable = job.status === 'succeeded' && Boolean(job.resultScenarioId)

  return (
    <div
      className={`bg-card border-l-[5px] ${spine} transition-all duration-100`}
    >
    <button
      onClick={onOpen}
      className="press-soft w-full p-3.5 text-left active:translate-x-[3px] active:translate-y-[3px] active:shadow-none transition-all duration-100"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[15px] font-extrabold text-text-primary truncate">
          {job.sourceFilename || '未命名文件'}
        </span>
        <StatusBadge status={job.status} />
      </div>

      {isJobRunning(job) && <ImportProgress job={job} compact />}

      {job.status === 'succeeded' && (
        <p className="text-[10.5px] text-text-muted mt-1">
          {job.nodeCount} 个场景 · {job.npcCount} 个人物
          {/* 🔴 降级交付的标记也要留在列表里：只在导入那一刻显示等于没显示，
              开局前想起来"这份好像有点问题"时，得能查得到。 */}
          {job.failureKinds.length > 0 && (
            <span className="text-brass-bright font-bold">
              {' '}
              · {job.failureKinds.length} 处提醒
            </span>
          )}
        </p>
      )}

      {(job.status === 'failed' || job.status === 'interrupted') && job.errorMessage && (
        <p className="text-[11px] text-text-muted mt-1 leading-[1.7] line-clamp-2">
          {job.errorMessage}
        </p>
      )}
    </button>

    {removable && !confirming && (
      <div className="px-3.5 pb-2.5 -mt-1">
        <button
          onClick={onAskDelete}
          className="press px-2 py-1 text-[11px] font-bold text-text-muted active:text-rust"
        >
          删掉这份
        </button>
      </div>
    )}

    {removable && confirming && (
      <DeleteConfirm deleting={deleting} onConfirm={onDelete} onCancel={onCancelDelete} />
    )}
    </div>
  )
}

/**
 * 删除确认条。**代价要写在按下之前**（同「我的房间」那条）。
 *
 * 模组删掉之后，用它开过的房间不受影响——因为**有房间在用就根本删不掉**
 * （后端 409）。所以这里要说的代价只有一条：这份转换结果没了，再要就得
 * 重传重转，那是十几分钟加一次钱。
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
        删了要再玩就得<span className="font-bold text-text-primary">重传重转一遍</span>
        （十几分钟）。已经用它开过的房间不受影响。
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

function StatusBadge({ status }: { status: string }) {
  if (status === 'succeeded') return <Badge variant="success">已就绪</Badge>
  if (status === 'failed') return <Badge variant="warning">没转成</Badge>
  if (status === 'interrupted') return <Badge variant="info">中断了</Badge>
  return <Badge variant="default">转换中</Badge>
}
