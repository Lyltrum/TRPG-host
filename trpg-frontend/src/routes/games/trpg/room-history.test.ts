import { describe, expect, it } from 'vitest'

import { mergeRoomHistory, shouldShowThinking } from './room-history'

const narr = (content: string) => ({ type: 'narr', content })
const system = (content: string) => ({ type: 'system', content })

describe('进房时把历史与实时消息合成一条时间线', () => {
  it('🔴 实时到的那条不许被历史覆盖掉', () => {
    // 真人实测 2026-08-11 多人局：开场叙事在 boot 轮询期间由 WS 送到，
    // 而 boot 最后一步是整体覆盖 → 那条被抹掉，且因为 eventId 已进去重表
    // **再也加不回来**，「守秘人正在思考」就一直亮着，只能刷新。
    const history = [system('案件档案已加载')]
    const live = [narr('周日晚间的暮色沉入窗框……')]

    const merged = mergeRoomHistory(history, live)

    expect(merged).toHaveLength(2)
    expect(merged[1]).toBe(live[0])
  })

  it('历史排在实时之前——live 里每一条都发生在轮询开始之后', () => {
    const merged = mergeRoomHistory([system('a'), narr('b')], [narr('c')])
    expect(merged.map((m) => m.content)).toEqual(['a', 'b', 'c'])
  })

  it('两边都空时不凭空造消息', () => {
    expect(mergeRoomHistory([], [])).toEqual([])
  })

  it('「正在思考」看的是合并之后有没有叙事', () => {
    // boot 自己那一份没有叙事，但 WS 已经送到了 → 不该点亮
    expect(shouldShowThinking(mergeRoomHistory([system('a')], [narr('b')]))).toBe(false)
    // 两边都没有叙事 → 该点亮（守秘人确实还在写）
    expect(shouldShowThinking(mergeRoomHistory([system('a')], []))).toBe(true)
  })
})
