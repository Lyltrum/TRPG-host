import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Trash2, UserRound } from 'lucide-react'
import type { CharacterTemplate } from 'trpg-sdk'
import ShellPage from '@/shared/components/ShellPage'
import { deleteMyTemplate, listMyTemplates } from '@/services/character/character-api'
import { templateSubtitle } from '@/services/character/character-view'
import { friendlyErrorMessage } from '@/services/api-client'

/**
 * 我的调查员（常用角色卡库）。
 *
 * 🔴 入口在**首页顶层**，跟「我的模组」同一个理由：卡库是**备料**，跟开局在
 * 时间上是分开的——老玩家会提前捏好卡，聚会那晚直接拿来用。只挂在建卡向导
 * 里的话，存进去之后就再也看不到它了（2026-08-13 真机反馈：「我确实能存进
 * 去，但是人物卡在哪里看呢？」）。
 *
 * 这一页只做**列表和删**：点某张卡进详情页（看全部信息 + 改文字）。
 * 「用这张卡开局」不在这里——那要先有房间，入口在建卡向导第一步。
 */

/** 属性摘要：八维里挑最能辨认一张卡的几项，不做完整卡片。 */
const SUMMARY_ATTRS: [string, string][] = [
  ['STR', '力量'],
  ['CON', '体质'],
  ['DEX', '敏捷'],
  ['INT', '智力'],
  ['POW', '意志'],
  ['EDU', '教育'],
]

function attrChips(template: CharacterTemplate): { label: string; value: number }[] {
  const data = (template.data ?? {}) as Record<string, unknown>
  const attrs = data.attributes
  if (!attrs || typeof attrs !== 'object') return []
  const table = attrs as Record<string, unknown>
  return SUMMARY_ATTRS.flatMap(([key, label]) => {
    const value = table[key]
    return typeof value === 'number' ? [{ label, value }] : []
  })
}

export default function MyCharactersPage() {
  const navigate = useNavigate()
  const [templates, setTemplates] = useState<CharacterTemplate[] | null>(null)
  const [error, setError] = useState('')
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [confirmingId, setConfirmingId] = useState<string | null>(null)

  useEffect(() => {
    listMyTemplates()
      .then(setTemplates)
      .catch((err) => {
        setError(friendlyErrorMessage(err))
        setTemplates([])
      })
  }, [])

  const handleDelete = async (templateId: string) => {
    setDeletingId(templateId)
    setError('')
    try {
      await deleteMyTemplate(templateId)
      setTemplates((prev) => (prev ?? []).filter((t) => t.templateId !== templateId))
    } catch (err) {
      setError(friendlyErrorMessage(err))
    } finally {
      setDeletingId(null)
      setConfirmingId(null)
    }
  }

  return (
    <ShellPage title="我的调查员" onBack={() => navigate('/home')} align="top">
      <div className="px-5 flex flex-col gap-5">
        {error && <p className="text-[11.5px] text-rust-dark text-center">{error}</p>}

        {templates === null && !error && (
          <p className="text-center text-[13px] text-text-dim py-10">加载中…</p>
        )}

        {templates !== null && templates.length === 0 && (
          <div className="text-center py-14 flex flex-col gap-4">
            <p className="text-[13px] text-text-muted">卡库还是空的</p>
            {/* 说清楚怎么才会有——不然玩家不知道这一页要怎么用 */}
            <p className="text-[11.5px] text-text-dim leading-relaxed px-6">
              建完一张角色卡之后，在「人物卡准备」那一屏点
              <span className="text-text-muted font-semibold">「存卡」</span>
              ，这张调查员就会进到这里。
              <br />
              下次开局建卡时，第一步就能直接拿来用。
            </p>
            <div className="flex flex-col gap-2.5 px-6">
              <button
                onClick={() => navigate('/home/create')}
                className="press w-full py-3 flex items-center justify-center gap-2 text-[13.5px] font-extrabold tracking-[0.12em] bg-ink-blue text-[#fff5ea]"
              >
                <Plus className="w-[16px] h-[16px]" /> 创建房间
              </button>
            </div>
          </div>
        )}

        {templates !== null && templates.length > 0 && (
          <div className="flex flex-col gap-2.5">
            {templates.map((template) => {
              const chips = attrChips(template)
              const confirming = confirmingId === template.templateId
              return (
                <div key={template.templateId} className="press-soft bg-card p-3 flex flex-col gap-2">
                  <div className="flex items-center gap-3">
                    <div className="w-[34px] h-[34px] flex-none flex items-center justify-center bg-page">
                      <UserRound className="w-[18px] h-[18px] text-text-muted" />
                    </div>
                    {/* 🔴 只有姓名那一块可点进详情，不把整张卡片做成按钮：删除键
                        在同一行里，整卡可点会把它吞掉（点删除先触发跳转）。 */}
                    <div
                      role="button"
                      tabIndex={0}
                      onClick={() => navigate(`/home/characters/${template.templateId}`)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ')
                          navigate(`/home/characters/${template.templateId}`)
                      }}
                      className="flex-1 min-w-0 cursor-pointer"
                    >
                      <div className="text-[13.5px] font-bold text-text-primary truncate">
                        {template.name}
                      </div>
                      <div className="text-[10.5px] text-text-muted mt-0.5 truncate">
                        {templateSubtitle(template)}
                      </div>
                    </div>
                    {/* 删除要二次确认：卡库里的卡是玩家攒下来的，误删没有撤销 */}
                    {confirming ? (
                      <div className="flex items-center gap-1.5 flex-none">
                        <button
                          onClick={() => void handleDelete(template.templateId)}
                          disabled={deletingId === template.templateId}
                          className="press px-2.5 py-1.5 text-[11.5px] font-bold bg-rust text-[#fff5ea] disabled:opacity-60 whitespace-nowrap"
                        >
                          {deletingId === template.templateId ? '删除中…' : '确认删除'}
                        </button>
                        <button
                          onClick={() => setConfirmingId(null)}
                          className="press px-2.5 py-1.5 text-[11.5px] font-bold bg-page text-text-muted whitespace-nowrap"
                        >
                          取消
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setConfirmingId(template.templateId)}
                        aria-label={`删除 ${template.name}`}
                        className="press flex-none p-1.5 text-text-muted"
                      >
                        <Trash2 className="w-[15px] h-[15px]" />
                      </button>
                    )}
                  </div>

                  {chips.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {chips.map((chip) => (
                        <span
                          key={chip.label}
                          className="px-1.5 py-0.5 text-[10px] bg-page text-text-muted"
                        >
                          {chip.label} {chip.value}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </ShellPage>
  )
}
