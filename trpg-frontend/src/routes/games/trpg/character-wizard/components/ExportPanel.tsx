import { useMemo, useState } from 'react'
import type { Character, CharacterComputeResult, SkillSpec } from 'trpg-sdk'
import { formatCharacterJson, formatDicebotFull, formatDicebotShort, formatTextCard } from 'trpg-sdk'

type ExportFormatKey = 'dicebotFull' | 'dicebotShort' | 'textCard' | 'json'
const EXPORT_FORMATS: Array<{ key: ExportFormatKey; label: string }> = [
  { key: 'dicebotFull', label: '骰娘·完整' },
  { key: 'dicebotShort', label: '骰娘·精简' },
  { key: 'textCard', label: '文本卡' },
  { key: 'json', label: 'JSON' },
]

/** 导出面板：格式下拉 + 生成 + 复制 + 只读 textarea，复用
 * trpg-sdk 的四个纯格式化函数（重制设计 v2 §6-步骤7 第 4 条）。 */
export function ExportPanel({
  character,
  preview,
  skills,
}: {
  character: Character | null
  preview: CharacterComputeResult | null
  skills: SkillSpec[]
}) {
  const [format, setFormat] = useState<ExportFormatKey>('dicebotFull')
  const [copyStatus, setCopyStatus] = useState('')

  const exportText = useMemo(() => {
    if (!character || !preview) return ''
    switch (format) {
      case 'dicebotFull':
        return formatDicebotFull(character, preview, skills)
      case 'dicebotShort':
        return formatDicebotShort(character, preview, skills)
      case 'textCard':
        return formatTextCard(character, preview, skills)
      case 'json':
        return formatCharacterJson(character, preview)
      default:
        return ''
    }
  }, [character, preview, skills, format])

  const handleCopy = async () => {
    if (!exportText) return
    try {
      await navigator.clipboard.writeText(exportText)
      setCopyStatus('已复制')
    } catch {
      setCopyStatus('复制失败，请手动选择文本')
    }
    setTimeout(() => setCopyStatus(''), 2000)
  }

  if (!character || !preview) {
    return <p className="text-[11px] text-text-muted">等待规则计算完成后可导出…</p>
  }

  return (
    <>
      <div className="flex gap-1.5 mb-2.5">
        {EXPORT_FORMATS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFormat(f.key)}
            className={`flex-1 py-1.5 text-[11px] font-semibold rounded-[6px] transition-all ${
              format === f.key ? 'bg-brass text-white' : 'bg-input border border-border-light text-text-muted'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>
      <textarea
        readOnly
        value={exportText}
        rows={6}
        className="w-full px-3 py-2.5 rounded-[6px] bg-input border border-border-light text-text-primary text-[11px] font-mono outline-none resize-none"
      />
      <button
        onClick={() => void handleCopy()}
        className="mt-2 w-full py-2 rounded-sm text-[12px] font-semibold border border-border-mid bg-panel text-text-body active:bg-card transition-all"
      >
        {copyStatus || '复制到剪贴板'}
      </button>
    </>
  )
}
