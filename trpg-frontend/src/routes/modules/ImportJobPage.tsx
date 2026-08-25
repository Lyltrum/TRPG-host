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
    // 🔴 换了 job 就不再是"提交中"。
    //
    // 重试成功后走的是 `navigate` 到新 jobId，而路由模式没变
    // （`/home/modules/:jobId`）——**组件实例被复用，state 不会重置**。
    // `busy` 原来只在 catch 里放开，于是成功路径上它永远是 true，按钮从第一次
    // 重试之后就永久禁用、卡在「重新提交中…」。真机第一轮就撞上了：用户点第二次
    // 重试，请求根本没发出去，后端日志里只有一条 retry。
    setBusy(false)
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

      {/* 🔴 降级交付：收下了，但有几处要提醒。判据在后端
          `job_state.DEGRADABLE_FAILURE_KINDS`——能玩的模组不该因为几处小瑕疵
          被整个扔掉，但玩家得知道是哪几处，撞上了才不会以为是自己看漏了。 */}
      {job.failureKinds.length > 0 && <Caveats kinds={job.failureKinds} />}

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

/** 带瑕疵交付时的提醒。**说清楚是什么、以及主持人会怎么处理**，不只是报个类别。 */
const CAVEAT_HINTS: Record<string, string> = {
  trace: '有几段内容找不回原文出处，守秘人不会把它们当成剧本里的事实',
  reach: '有一幕可能没有入口，玩家不一定走得到',
  orphan: '有几段内容没有归位，这一局里可能不会出现',
  thin_slot: '开场给的信息偏多',
  skill: '有检定的技能名对不上，那几处检定已经去掉',
}

function Caveats({ kinds }: { kinds: string[] }) {
  return (
    <div className="press-soft bg-card p-3.5 mt-3 border-l-[5px] border-l-brass-bright">
      <p className="text-[10.5px] font-bold text-brass-bright tracking-[0.1em]">
        收下了，但有 {kinds.length} 处要提醒
      </p>
      <div className="h-[1.5px] bg-text-primary/20 my-2.5" />
      <ul className="flex flex-col gap-1.5">
        {kinds.map((kind) => (
          <li key={kind} className="text-[11.5px] text-text-muted leading-[1.7]">
            · {CAVEAT_HINTS[kind] ?? FAILURE_KIND_LABELS[kind] ?? kind}
          </li>
        ))}
      </ul>
      <p className="text-[10.5px] text-text-dim mt-2.5 leading-[1.7]">
        这些不影响开局。真撞上了，守秘人会当场圆过去。
      </p>
    </div>
  )
}

function Counts({ job }: { job: ModuleImportJob }) {
  // 🔴 `number | null` 而不是 `number`：null = **这次导入没有量过这个数**
  // （本次改动之前的所有 job），0 = 量过、确实是零。两者含义相反，
  // 压成一个值就是在骗人（`exec/46` B1）。
  const rows: [string, number | null][] = [
    ['场景', job.nodeCount],
    ['登场人物', job.npcCount],
    ['结局', job.endingCount],
    ['时间线事件', job.agendaCount],
    ['线索条目', job.factCount],
    ['能挣到线索的检定点', job.revealingCheckCount],
  ]
  // 账本是死的：有线索却没有一个检定点指向它们，跟压根没有线索一样——
  // 玩家永远挣不到任何一条。两种都要说出口。
  const ledgerDead =
    job.factCount === 0 || (job.factCount !== null && job.revealingCheckCount === 0)
  return (
    <div className="flex flex-col">
      {rows.map(([k, v]) => (
        <div
          key={k}
          className="flex items-center justify-between py-1 border-b border-dotted border-text-primary/25 last:border-b-0"
        >
          <span className="text-[12.5px] text-text-muted">{k}</span>
          <span className="text-[12.5px] font-extrabold font-mono text-text-primary">
            {v === null ? '—' : v}
          </span>
        </div>
      ))}
      {ledgerDead && (
        // 🔴 说出后果，不只说数字。真机撞到过：一份 facts=0 的模组跑了 265 拍，
        // 线索账本零记账，而在此之前没有任何地方看得出来。
        <p className="text-[10.5px] text-rust mt-2.5 leading-[1.8] border-l-2 border-rust pl-2">
          这份模组<b>没有能被挣到的线索</b>：对局里「现场」抽屉的线索会一直是空的，
          收尾时的「没查到什么」也统计不出东西。故事照样能跑，但线索这条线是断的
          —— 换一份文件重导可能会好。
        </p>
      )}
    </div>
  )
}
