import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Upload } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import { startModuleImport } from '@/services/module-import'

/** 跟后端 `ALLOWED_SUFFIXES` 一致。前端只是提前挡一道，权威在后端。 */
const ACCEPT = '.pdf,.docx,.doc,.txt'

export default function ImportModulePage() {
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async () => {
    if (!file || submitting) return
    setSubmitting(true)
    setError('')
    try {
      const job = await startModuleImport(file)
      navigate(`/home/modules/${job.jobId}`, { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传失败')
      setSubmitting(false)
    }
  }

  return (
    <ShellPage title="导入模组" onBack={() => navigate('/home/modules')} align="top">
      <div className="px-5">
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null)
            setError('')
          }}
        />

        <button
          onClick={() => inputRef.current?.click()}
          className="w-full border-2 border-dashed border-text-primary/45 py-8 px-4 flex flex-col items-center gap-2 active:bg-card transition-all"
        >
          <Upload className="w-7 h-7 text-text-primary" strokeWidth={2} />
          <span className="text-[14px] font-extrabold text-text-primary">
            {file ? file.name : '选择模组文件'}
          </span>
          <span className="text-[10.5px] text-text-dim">
            {file ? '点一下换一个' : 'PDF / DOCX / DOC / TXT'}
          </span>
        </button>

        {/* 🔴 这张便签是这个功能的**卖点声明**，不是免责声明——「没有人会读它」
            正是它跟"找个人先过一遍"的区别，也是整个导入功能的第一性约束。 */}
        <div className="press-soft bg-card p-3 mt-4">
          <p className="text-[10.5px] font-mono font-bold text-rust tracking-[0.14em]">
            没有人会读它
          </p>
          <p className="text-[12.5px] text-text-body mt-1.5 leading-[1.75]">
            转换全程由机器完成。<b>我们不会把模组内容显示给任何人</b>，也包括你——
            这样你和朋友坐下来时，桌上仍然没有人知道故事怎么走。
          </p>
        </div>

        <ul className="text-[11.5px] text-text-muted mt-4 leading-[1.85] list-none">
          <li>· 一次一个文件，压缩包请先解开</li>
          <li>· 扫描版（图片 PDF）目前读不了，需要文字版</li>
          <li>
            · 转换大约 <b>5–25 分钟</b>，可以关掉页面
          </li>
        </ul>

        {error && <p className="text-[12.5px] text-rust font-bold mt-4">{error}</p>}

        <button
          onClick={() => void submit()}
          disabled={!file || submitting}
          className="press w-full py-3 mt-6 mb-6 bg-rust text-[#fff5ea] text-[14.5px] font-extrabold disabled:opacity-45"
        >
          {submitting ? '上传中…' : '开始转换'}
        </button>
      </div>
    </ShellPage>
  )
}
