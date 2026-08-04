import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import ShellPage from '@/shared/components/ShellPage'
import ImportProgress from './ImportProgress'
import {
  FAILURE_KIND_LABELS,
  getModuleImport,
  isJobRunning,
  retryModuleImport,
  type ModuleImportJob,
} from '@/services/module-import'
import { useGameStore } from '@/stores/game-store'

const POLL_MS = 4000

/** 一次导入的全过程：转换中 / 成功 / 拒绝 / 中断，四种状态一屏。 */
export default function ImportJobPage() {
  const navigate = useNavigate()
  const { jobId } = useParams<{ jobId: string }>()
  const [job, setJob] = useState<ModuleImportJob | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (id: string) => {
    try {
      setJob(await getModuleImport(id))
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取导入任务失败')
    }
  }, [])

  useEffect(() => {
    if (jobId) void load(jobId)
  }, [jobId, load])

  const running = job ? isJobRunning(job) : false
  useEffect(() => {
    if (!running || !jobId) return
    const timer = window.setInterval(() => void load(jobId), POLL_MS)
    return () => window.clearInterval(timer)
  }, [running, jobId, load])

  const retry = async () => {
    if (!jobId || busy) return
    setBusy(true)
    try {
      // 🔴 重跑返回的是**一个新 job**，要跳到新的 id 上继续看。
      // 停在旧 id 上会一直显示那次的失败，看起来像"按钮没反应"。
      const next = await retryModuleImport(jobId)
      navigate(`/home/modules/${next.jobId}`, { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : '重试失败')
      setBusy(false)
    }
  }

  return (
    <ShellPage title="导入模组" onBack={() => navigate('/home/modules')} align="top">
      <div className="px-5 pb-6">
        {error && <p className="text-[12.5px] text-rust font-bold mb-3">{error}</p>}
        {!job && !error && <p className="text-[12.5px] text-text-muted">读取中…</p>}

        {job && (
          <>
            <div className="press-soft bg-card p-3.5">
              <p className="text-[12.5px] font-extrabold text-text-primary truncate">
                {job.sourceFilename || '未命名文件'}
              </p>
              <p className="text-[10.5px] text-text-dim mt-0.5">
                {job.pageCount > 0 ? `${job.pageCount} 页 · ` : ''}
                {job.status === 'succeeded' ? '已收入模组库' : '已上传'}
              </p>
            </div>

            {job.status !== 'succeeded' && (
              <div className="mt-4">
                <ImportProgress job={job} />
              </div>
            )}

            {running && (
              <p className="text-[11.5px] text-text-muted mt-4 leading-[1.8]">
                还要几分钟。<b>可以关掉这个页面</b>——转好了它会出现在模组列表里。
              </p>
            )}

            {job.status === 'succeeded' && <Succeeded job={job} navigate={navigate} />}
            {job.status === 'failed' && <Failed job={job} onRetry={retry} busy={busy} navigate={navigate} />}
            {job.status === 'interrupted' && (
              <Interrupted job={job} onRetry={retry} busy={busy} navigate={navigate} />
            )}
          </>
        )}
      </div>
    </ShellPage>
  )
}

type Nav = ReturnType<typeof useNavigate>

function Succeeded({ job, navigate }: { job: ModuleImportJob; navigate: Nav }) {
  const setScene = useGameStore((s) => s.setScene)

  const play = () => {
    if (!job.resultScenarioId) return
    // 模组已经能开局了，但它属于哪个游戏/规则系统由创建页那条既有流程决定；
    // 这里只把"选了哪个模组"记下来。
    setScene(job.resultScenarioId, job.sourceFilename || '导入的模组')
    navigate('/home/create')
  }

  return (
    <>
      <div className="press-soft bg-card p-3.5 mt-4">
        <p className="text-[10.5px] font-bold text-mold tracking-[0.1em]">已收入模组库</p>
        <div className="h-[1.5px] bg-text-primary/20 my-2.5" />
        <Counts job={job} />
      </div>

      {/* 🔴 这句要说出口：报告只有数字**是有意的**，不是我们没做完。 */}
      <p className="text-[10.5px] text-text-dim mt-3 leading-[1.8]">
        只显示数量。<b>模组的内容一个字也不会展示</b>，这样开局时它对所有人都是新的。
      </p>

      <button
        onClick={play}
        className="press w-full py-3 mt-5 bg-rust text-[#fff5ea] text-[14.5px] font-extrabold"
      >
        用它开一局
      </button>
      <button
        onClick={() => navigate('/home/modules')}
        className="press w-full py-3 mt-2.5 bg-card text-text-primary text-[14.5px] font-extrabold"
      >
        回到模组列表
      </button>
    </>
  )
}

function Failed({
  job,
  onRetry,
  busy,
  navigate,
}: {
  job: ModuleImportJob
  onRetry: () => void
  busy: boolean
  navigate: Nav
}) {
  return (
    <>
      <div className="press-soft bg-card p-3.5 mt-4 border-l-[5px] border-l-rust">
        <p className="text-[13.5px] font-extrabold text-text-primary leading-[1.7]">
          {job.errorMessage || '没能转成。'}
        </p>
        <p className="text-[12.5px] text-text-muted mt-1.5 leading-[1.75]">
          这份模组转出来的结果不够可靠，我们没有收下它——宁可不给你，也不给你一个会在半路出错的模组。
        </p>
        {job.failureKinds && job.failureKinds.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2.5">
            {job.failureKinds.map((kind) => (
              <span
                key={kind}
                className="inline-block px-2 py-[1px] border-2 border-rust text-rust-dark text-[10.5px] font-bold"
              >
                {FAILURE_KIND_LABELS[kind] ?? kind}
              </span>
            ))}
          </div>
        )}
      </div>

      <button
        onClick={onRetry}
        disabled={busy}
        className="press w-full py-3 mt-5 bg-rust text-[#fff5ea] text-[14.5px] font-extrabold disabled:opacity-45"
      >
        {busy ? '重新提交中…' : '再试一次'}
      </button>
      {/* 🔴 这句不能省：不解释的话，重试按钮看起来像"再撞一次墙"。
          而组装输出高度不稳定——同一份文件跑两次问题类别可能完全不同。 */}
      <p className="text-[10.5px] text-text-dim text-center mt-1.5 leading-[1.7]">
        每次转换的结果会不一样，重试确实有机会成功。
      </p>
      <button
        onClick={() => navigate('/home/modules/import')}
        className="press w-full py-3 mt-3 bg-card text-text-primary text-[14.5px] font-extrabold"
      >
        换一份文件
      </button>
    </>
  )
}

function Interrupted({
  job,
  onRetry,
  busy,
  navigate,
}: {
  job: ModuleImportJob
  onRetry: () => void
  busy: boolean
  navigate: Nav
}) {
  return (
    <>
      <div className="press-soft bg-card p-3.5 mt-4 border-l-[5px] border-l-ink-blue">
        <p className="text-[13.5px] font-extrabold text-text-primary leading-[1.7]">
          {job.errorMessage || '这次转换没跑完。'}
        </p>
        {/* 🔴 这一屏存在的全部理由就是这句：跟失败合并的话，用户会以为自己找的
            模组有问题、跑去换文件——而实际上只要点一下就能重来。 */}
        <p className="text-[12.5px] text-text-muted mt-1.5 leading-[1.75]">
          <b>跟你的模组没关系。</b>文件还在服务器上，点一下就能重来，不用再传一次。
        </p>
      </div>

      {/* 🔴 「从头重转」不能写成「接着转」：后端的重跑是新建 job、整条链从头跑，
          没有断点续传。写错了用户会以为只剩最后两步。 */}
      <p className="text-[10.5px] text-text-dim mt-3 leading-[1.8]">
        会从头重转一遍（没有断点续传），还是十几分钟。
      </p>

      <button
        onClick={onRetry}
        disabled={busy}
        className="press w-full py-3 mt-4 bg-rust text-[#fff5ea] text-[14.5px] font-extrabold disabled:opacity-45"
      >
        {busy ? '重新提交中…' : '重新转一次'}
      </button>
      <button
        onClick={() => navigate('/home/modules')}
        className="press w-full py-3 mt-2.5 bg-card text-text-primary text-[14.5px] font-extrabold"
      >
        回到模组列表
      </button>
    </>
  )
}

function Counts({ job }: { job: ModuleImportJob }) {
  const rows: [string, number][] = [
    ['场景', job.nodeCount],
    ['登场人物', job.npcCount],
    ['结局', job.endingCount],
    ['时间线事件', job.agendaCount],
  ]
  return (
    <div className="flex flex-col">
      {rows.map(([k, v]) => (
        <div
          key={k}
          className="flex items-center justify-between py-1 border-b border-dotted border-text-primary/25 last:border-b-0"
        >
          <span className="text-[12.5px] text-text-muted">{k}</span>
          <span className="text-[12.5px] font-extrabold font-mono text-text-primary">{v}</span>
        </div>
      ))}
    </div>
  )
}
