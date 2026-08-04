import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Upload } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import Badge from '@/shared/components/Badge'
import ImportProgress from './ImportProgress'
import {
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
          <JobCard key={job.jobId} job={job} onOpen={() => navigate(`/home/modules/${job.jobId}`)} />
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

function JobCard({ job, onOpen }: { job: ModuleImportJob; onOpen: () => void }) {
  const spine = SPINE[job.status] ?? SPINE.interrupted

  return (
    <button
      onClick={onOpen}
      className={`press-soft bg-card p-3.5 text-left border-l-[5px] ${spine} active:translate-x-[3px] active:translate-y-[3px] active:shadow-none transition-all duration-100`}
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
        </p>
      )}

      {(job.status === 'failed' || job.status === 'interrupted') && job.errorMessage && (
        <p className="text-[11px] text-text-muted mt-1 leading-[1.7] line-clamp-2">
          {job.errorMessage}
        </p>
      )}
    </button>
  )
}

function StatusBadge({ status }: { status: string }) {
  if (status === 'succeeded') return <Badge variant="success">已就绪</Badge>
  if (status === 'failed') return <Badge variant="warning">没转成</Badge>
  if (status === 'interrupted') return <Badge variant="info">中断了</Badge>
  return <Badge variant="default">转换中</Badge>
}
