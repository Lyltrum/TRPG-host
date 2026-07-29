// 年龄修正表（COC7 守秘人规则书 · 创造调查员 / 年龄，迁移自 coc-char-gen
// `js/plugins/age.js`），纯展示用的静态数据——真正的计算权威在后端
// `apply-age-adjustment` 端点。
//
// ⚠️ 这是后端 `coc7_age.py::AGE_TABLE` 的展示副本（重制设计 v2 §4-C 推迟
// 决定），改后端要同步改这里，两边不共享同一份数据源。
const AGE_BAND_TABLE: Array<{
  minAge: number
  maxAge: number
  range: string
  edu: string
  body: string
  other: string
}> = [
  { minAge: 15, maxAge: 19, range: '15–19', edu: '固定 −5', body: 'STR 与 SIZ 合计 −5', other: '幸运掷两次取高' },
  { minAge: 20, maxAge: 39, range: '20–39', edu: '改进检定 ×1', body: '—', other: '—' },
  { minAge: 40, maxAge: 49, range: '40–49', edu: '改进检定 ×2', body: 'STR+CON+DEX 共 −5，APP−5', other: 'MOV−1' },
  { minAge: 50, maxAge: 59, range: '50–59', edu: '改进检定 ×3', body: '共 −10，APP−10', other: 'MOV−2' },
  { minAge: 60, maxAge: 69, range: '60–69', edu: '改进检定 ×4', body: '共 −20，APP−15', other: 'MOV−3' },
  { minAge: 70, maxAge: 79, range: '70–79', edu: '改进检定 ×4', body: '共 −40，APP−20', other: 'MOV−4' },
  { minAge: 80, maxAge: 89, range: '80–89', edu: '改进检定 ×4', body: '共 −80，APP−25', other: 'MOV−5' },
]

/** 一行白话小结（照搬 coc-char-gen 的 plainAgeNote，§6-步骤2 第 3 条）。 */
export function describeAgeBand(age: number): string {
  const row = AGE_BAND_TABLE.find((r) => age >= r.minAge && age <= r.maxAge)
  if (!row) return ''
  const parts = [`教育：${row.edu}`]
  if (row.body !== '—') parts.push(row.body)
  if (row.other !== '—') parts.push(row.other)
  return parts.join('；')
}

/** 7 行年龄档表格，当前年龄所在行高亮（重制设计 v2 §6-步骤2）。 */
export function AgeBandTable({ age }: { age: number }) {
  return (
    <div>
      <p className="text-[10px] text-text-dim mb-1.5">
        以下是后端 coc7_age.py::AGE_TABLE 的展示副本，仅供参考——真正生效的规则由套用按钮调用后端计算。
      </p>
      <div className="overflow-x-auto -mx-1">
        <table className="w-full text-[11px] border-collapse min-w-[420px]">
          <thead>
            <tr className="text-text-dim">
              <th className="text-left py-1.5 px-1.5 font-semibold">年龄段</th>
              <th className="text-left py-1.5 px-1.5 font-semibold">EDU</th>
              <th className="text-left py-1.5 px-1.5 font-semibold">身体/外貌</th>
              <th className="text-left py-1.5 px-1.5 font-semibold">其他</th>
            </tr>
          </thead>
          <tbody>
            {AGE_BAND_TABLE.map((row) => {
              const active = age >= row.minAge && age <= row.maxAge
              return (
                <tr key={row.range} className={`border-t border-border-light ${active ? 'bg-[#fdfaf4]' : ''}`}>
                  <td className={`py-1.5 px-1.5 font-mono ${active ? 'text-brass-dark font-bold' : 'text-text-primary'}`}>
                    {row.range}
                  </td>
                  <td className="py-1.5 px-1.5 text-text-body">{row.edu}</td>
                  <td className="py-1.5 px-1.5 text-text-body">{row.body}</td>
                  <td className="py-1.5 px-1.5 text-text-body">{row.other}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
