import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import ShellPage from '@/shared/components/ShellPage'
import { useRuleset } from '@/hooks/useRuleset'
import {
  BACKGROUND_DETAIL_FIELDS,
  emptyBackgroundDetail,
  type BackgroundDetail,
} from '@/data/character-model'
import { fetchMyTemplate, updateMyTemplate } from '@/services/character/character-api'
import { friendlyErrorMessage } from '@/services/api-client'

/**
 * 卡库详情：把这张常用卡的**全部**信息摆出来，文字部分就地可改。
 *
 * ## 🔴 为什么规则数只读，而且不走建卡向导
 *
 * 用户 2026-08-13 定的：「不应该按照以前建卡引导的完整步骤来做，把所有信息
 * 展示出来，玩家编辑就行了。」而**属性/年龄/职业/技能改不了**是同一次讨论的
 * 另一半——它们改一处就要重跑整套 COC7 校验、年龄修正、两个技能点池的预算，
 * 而那条链路已经长在建卡向导上。在卡库里再造一套就是「同一件事有两个实现，
 * 换一个实现功能就悄悄没了」。
 *
 * 想改数值的路是走得通的，页面底部那句话就是它：用这张卡开局 → 在向导里改 →
 * 再存一张。**加一道门必须同时给它配一条走得通的修法。**
 */

/** 属性展示顺序：跟角色卡上的排布一致，不按字典序。 */
const ATTR_ORDER = ['STR', 'CON', 'SIZ', 'DEX', 'APP', 'INT', 'POW', 'EDU', 'LUCK']

/** 衍生值：只挑角色卡上真正印着的那几项。 */
const DERIVED_ORDER: [string, string][] = [
  ['HP', '体力'],
  ['MP', '魔法'],
  ['SAN', '理智'],
  ['MOV', '移动'],
  ['DB', '伤害加值'],
  ['Build', '体格'],
]

type TextPatch = {
  name: string
  gender: string
  residence: string
  birthplace: string
  equipment: string
  notes: string
  background: string
  backgroundDetail: BackgroundDetail
}

/** 装备在后端是 `list[str]`，这一页按一行文本编辑（同建卡向导那一步）。 */
function equipmentToText(value: unknown): string {
  return Array.isArray(value) ? value.filter((v) => typeof v === 'string').join('、') : ''
}

function equipmentToList(text: string): string[] {
  return text
    .split(/[,，、\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function readString(data: Record<string, unknown>, key: string): string {
  const value = data[key]
  return typeof value === 'string' ? value : ''
}

export default function MyCharacterDetailPage() {
  const navigate = useNavigate()
  const { templateId = '' } = useParams()
  const { ruleset } = useRuleset()

  const [data, setData] = useState<Record<string, unknown> | null>(null)
  const [cardName, setCardName] = useState('')
  const [text, setText] = useState<TextPatch | null>(null)
  const [loadError, setLoadError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!templateId) return
    let cancelled = false
    fetchMyTemplate(templateId)
      .then((template) => {
        if (cancelled) return
        const raw = (template.data ?? {}) as Record<string, unknown>
        setData(raw)
        setCardName(template.name)
        setText({
          name: readString(raw, 'name'),
          gender: readString(raw, 'gender'),
          residence: readString(raw, 'residence'),
          birthplace: readString(raw, 'birthplace'),
          equipment: equipmentToText(raw.equipment),
          notes: readString(raw, 'notes'),
          background: readString(raw, 'background'),
          // 八栏引导后端当不透明字典存，缺键是常态——补齐成完整形状再进表单，
          // 否则受控 textarea 会从 undefined 变成有值，React 当场报非受控警告。
          backgroundDetail: {
            ...emptyBackgroundDetail(),
            ...((raw.background_detail as Partial<BackgroundDetail> | null) ?? {}),
          },
        })
      })
      .catch((err) => {
        if (!cancelled) setLoadError(friendlyErrorMessage(err, '读不到这张卡'))
      })
    return () => {
      cancelled = true
    }
  }, [templateId])

  const attributes = (data?.attributes ?? {}) as Record<string, number>
  const derived = (data?.derived_stats ?? {}) as Record<string, number | string>

  /** 技能按值从高到低：一眼看出这张卡擅长什么，比按 id 排有用。 */
  const skillRows = useMemo(() => {
    const skills = (data?.skills ?? {}) as Record<string, number>
    const nameById = new Map((ruleset?.skills ?? []).map((s) => [s.id, s.name]))
    return Object.entries(skills)
      .map(([id, value]) => ({ id, value, name: nameById.get(id) ?? id }))
      .sort((a, b) => b.value - a.value)
  }, [data, ruleset])

  const attrLabel = (key: string) =>
    ruleset?.attributes.find((a) => a.key === key)?.label ?? key

  const edit = (patch: Partial<TextPatch>) => {
    setText((prev) => (prev ? { ...prev, ...patch } : prev))
    setSaved(false)
  }

  const handleSave = async () => {
    if (!text || !cardName.trim()) return
    setSaving(true)
    setSaveError('')
    try {
      const updated = await updateMyTemplate(templateId, {
        name: cardName.trim(),
        data: {
          name: text.name,
          gender: text.gender,
          residence: text.residence,
          birthplace: text.birthplace,
          equipment: equipmentToList(text.equipment),
          notes: text.notes,
          background: text.background,
          background_detail: text.backgroundDetail,
        },
      })
      setCardName(updated.name)
      setData((updated.data ?? {}) as Record<string, unknown>)
      setSaved(true)
      // 保存完就回卡库：这一页是"改这张卡"，改完这件事就结束了。停在原地会让人
      // 以为还没完（还得自己找返回键），而列表页正好能看到改后的标题与副标题。
      navigate('/home/characters')
    } catch (err) {
      setSaveError(friendlyErrorMessage(err, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <ShellPage title="调查员详情" onBack={() => navigate('/home/characters')} align="top">
      <div className="px-5 flex flex-col gap-3 pb-8">
        {loadError && <p className="text-[11.5px] text-rust-dark text-center py-8">{loadError}</p>}
        {!loadError && !text && (
          <p className="text-center text-[13px] text-text-dim py-10">加载中…</p>
        )}

        {text && (
          <>
            <Section title="卡库里的名字">
              <input
                value={cardName}
                onChange={(e) => {
                  setCardName(e.target.value)
                  setSaved(false)
                }}
                placeholder="给这张卡起个名字"
                className="shell-field w-full px-3 py-2 text-[14px]"
              />
            </Section>

            <Section title="调查员">
              <Field label="姓名" value={text.name} onChange={(v) => edit({ name: v })} />
              <Field label="性别" value={text.gender} onChange={(v) => edit({ gender: v })} />
              <Field
                label="居住地"
                value={text.residence}
                onChange={(v) => edit({ residence: v })}
              />
              <Field
                label="出生地"
                value={text.birthplace}
                onChange={(v) => edit({ birthplace: v })}
              />
              <ReadOnly label="年龄" value={typeof data?.age === 'number' ? `${data.age}` : '—'} />
              <ReadOnly label="职业" value={readString(data ?? {}, 'occupation') || '—'} />
            </Section>

            <Section title="属性">
              <div className="grid grid-cols-3 gap-1.5">
                {ATTR_ORDER.filter((k) => typeof attributes[k] === 'number').map((key) => (
                  <div key={key} className="bg-page px-2 py-1.5 flex flex-col items-center">
                    <span className="text-[10px] text-text-dim">{attrLabel(key)}</span>
                    <span className="text-[15px] font-bold text-text-primary">
                      {attributes[key]}
                    </span>
                  </div>
                ))}
              </div>
              <div className="flex flex-wrap gap-1 mt-2">
                {DERIVED_ORDER.filter(([key]) => derived[key] != null).map(([key, label]) => (
                  <span key={key} className="px-1.5 py-0.5 text-[10px] bg-page text-text-muted">
                    {label} {String(derived[key])}
                  </span>
                ))}
              </div>
            </Section>

            <Section title={`技能（${skillRows.length}）`}>
              {skillRows.length === 0 ? (
                <p className="text-[11.5px] text-text-dim">这张卡没有记录技能</p>
              ) : (
                <div className="flex flex-col">
                  {skillRows.map((skill) => (
                    <div
                      key={skill.id}
                      className="flex items-baseline justify-between py-1 border-b border-text-dim/15 last:border-0"
                    >
                      <span className="text-[12.5px] text-text-body">{skill.name}</span>
                      <span className="text-[12.5px] font-bold text-text-primary">
                        {skill.value}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Section>

            <Section title="装备与备注">
              <textarea
                value={text.equipment}
                onChange={(e) => edit({ equipment: e.target.value })}
                rows={2}
                placeholder="随身携带的东西，用顿号或换行分开"
                className="shell-field w-full px-3 py-2 text-[12.5px] leading-relaxed resize-y"
              />
              <textarea
                value={text.notes}
                onChange={(e) => edit({ notes: e.target.value })}
                rows={2}
                placeholder="备注：只有你自己看得到的memo"
                className="shell-field w-full px-3 py-2 text-[12.5px] leading-relaxed resize-y"
              />
            </Section>

            <Section title="背景故事">
              <textarea
                value={text.background}
                onChange={(e) => edit({ background: e.target.value })}
                rows={5}
                placeholder="他是谁、从哪来、为什么会卷进这件事…"
                className="shell-field w-full px-3 py-2 text-[13px] leading-relaxed resize-y"
              />
              {BACKGROUND_DETAIL_FIELDS.map((field) => (
                <div key={field.key} className="flex flex-col gap-1">
                  <label className="text-[10.5px] font-bold text-text-muted tracking-[0.1em]">
                    {field.label}
                  </label>
                  <textarea
                    value={text.backgroundDetail[field.key] ?? ''}
                    onChange={(e) =>
                      edit({
                        backgroundDetail: {
                          ...text.backgroundDetail,
                          [field.key]: e.target.value,
                        },
                      })
                    }
                    rows={2}
                    placeholder={field.placeholder}
                    className="shell-field w-full px-3 py-2 text-[12.5px] leading-relaxed resize-y"
                  />
                </div>
              ))}
            </Section>

            {/* 🔴 门配修法：数值改不了，但要当场告诉他改数值的路怎么走 */}
            <p className="text-[11px] text-text-dim leading-relaxed px-1">
              属性、年龄、职业、技能在这里只能看。想改这些数：用这张卡开一局，在建卡向导里改完，再存一张新的进卡库。
            </p>

            {saveError && <p className="text-[11.5px] text-rust-dark text-center">{saveError}</p>}

            <button
              onClick={() => void handleSave()}
              disabled={saving || !cardName.trim()}
              className="press w-full py-3 text-[13.5px] font-extrabold tracking-[0.12em] bg-ink-blue text-[#fff5ea] disabled:opacity-60"
            >
              {saving ? '保存中…' : saved ? '已保存' : '保存修改'}
            </button>
          </>
        )}
      </div>
    </ShellPage>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="press-soft bg-card p-3.5 flex flex-col gap-2">
      <span className="text-[10.5px] font-bold text-text-muted tracking-[0.1em]">{title}</span>
      {children}
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-[52px] flex-none text-[11.5px] text-text-muted">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="shell-field flex-1 min-w-0 px-3 py-1.5 text-[13px]"
      />
    </div>
  )
}

function ReadOnly({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-[52px] flex-none text-[11.5px] text-text-muted">{label}</span>
      <span className="flex-1 text-[13px] text-text-body">{value}</span>
    </div>
  )
}
