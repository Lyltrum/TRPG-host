import type { AgeAdjustmentResult } from 'trpg-sdk'

/** EDU 检定明细 / 减值 / 前后对照（重制设计 v2 §6-步骤2 第 4 条）。 */
export function AgeAdjustmentReport({ result }: { result: AgeAdjustmentResult }) {
  return (
    <div className="border border-ink/35 p-3 space-y-2.5">
      <h4 className="text-[12px] font-semibold text-brass-dark uppercase tracking-[0.08em]">
        调整结果 · {result.ageLabel} 岁段
      </h4>

      {result.eduChecks && result.eduChecks.length > 0 && (
        <div>
          <div className="text-[11px] font-semibold text-text-muted mb-1.5">EDU 改进检定</div>
          <div className="space-y-1">
            {result.eduChecks.map((c, i) => (
              <div
                key={i}
                className={`text-[11px] font-mono px-2.5 py-1.5 rounded ${
                  c.success ? 'border border-[#3d6b2f] text-[#3d6b2f]' : 'border border-ink/25 text-ink-soft'
                }`}
              >
                第{i + 1}次：d100={c.roll}
                {c.success ? ` ＞ ${c.eduBefore}，成功 +${c.gain}（${c.eduBefore}→${c.eduAfter}）` : ` ≤ ${c.eduBefore}，失败`}
              </div>
            ))}
          </div>
        </div>
      )}

      {!!result.eduFlatAdjustment && <div className="text-[11px] text-text-body">EDU 固定调整：{result.eduFlatAdjustment}</div>}
      {result.luckRerolled && <div className="text-[11px] text-text-body">幸运已按青年档规则双掷取高</div>}
      {!!result.scdLoss && (
        <div className="text-[11px] text-text-body">
          {(result.scdAffectedAttributes ?? []).join('/')} 合计减值 {result.scdLoss}
        </div>
      )}
      {!!result.appLoss && <div className="text-[11px] text-text-body">APP 减值 {result.appLoss}</div>}
      {!!result.movPenalty && <div className="text-[11px] text-text-body">MOV 惩罚 −{result.movPenalty}</div>}

      <div>
        <div className="text-[11px] font-semibold text-text-muted mb-1.5">属性调整前后对比</div>
        <div className="grid grid-cols-2 gap-1.5">
          {Object.keys(result.attributesAfter).map((key) => {
            const before = result.attributesBefore[key]
            const after = result.attributesAfter[key]
            const changed = before !== after
            return (
              <div
                key={key}
                className={`flex items-center justify-between px-2.5 py-1.5 rounded text-[11px] font-mono ${
                  changed ? 'border border-brass-dark bg-white/25' : 'border border-ink/20 bg-white/10'
                }`}
              >
                <span className="text-text-muted">{key}</span>
                <span className={changed ? 'text-brass-dark font-bold' : 'text-text-dim'}>
                  {before}
                  {changed && ` → ${after}`}
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
