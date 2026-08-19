import { describe, expect, it } from 'vitest'

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { appendOnce, mergeRoomHistory, shouldShowThinking } from './room-history'

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

// ── 待掷检定的重复推送（三人真机 2026-08-19）──────────────────────

describe('同一张待掷卡到达两次时只留一条', () => {
  const card = (key: string, content = '守秘人请求你进行侦察检定') => ({
    type: 'system' as const, content, dedupeKey: key,
  })

  it('🔴 多人局里后掷的那个人会收到两次同一张卡', () => {
    // 两个人同时被要求掷骰，先掷完的那个走结算 → pending 守卫把还没掷的人
    // 那张**重发一遍**。实测 C 在 0.03 秒内收到两条 checkRequestId 相同的
    // `check.request`（房间 4D5NP1 第 2 拍）。
    const once = appendOnce([], card('check-request:77482720'))
    const twice = appendOnce(once, card('check-request:77482720'))

    expect(twice).toHaveLength(1)
    expect(twice).toBe(once) // 同一个数组引用：React 不会因此重渲染
  })

  it('不同的卡照常各占一条——去重不许压掉真的第二次检定', () => {
    const list = appendOnce(appendOnce([], card('check-request:aaa')), card('check-request:bbb'))
    expect(list).toHaveLength(2)
  })

  it('理智检定与技能检定的键不共用命名空间', () => {
    // 两种卡的 checkRequestId 来自不同的序列，裸 id 当键会互相压掉。
    const list = appendOnce(appendOnce([], card('check-request:x')), card('san-check-request:x'))
    expect(list).toHaveLength(2)
  })

  it('没有 dedupeKey 的照常追加（历史重建、重连补发走的是这条路）', () => {
    // 形状跟真实的 `Message` 一致：字段声明了、只是这一条没赋值。
    const plain: { type: 'system'; content: string; dedupeKey?: string } =
      { type: 'system', content: '你已重新入座' }
    const list = appendOnce(appendOnce([], plain), plain)
    expect(list).toHaveLength(2)
  })
})

describe('🔴 接线：RoomPage 的两个待掷分支真的用了它', () => {
  // **只测函数不测接线** = 「加了函数没有消费方」。这条读的是源码本身：
  // 把调用点改回 `[...prev, {...}]` 或把 setPendingCheck 改回无条件覆盖，
  // 上面四条纯函数测试**全都照样绿**，只有这一条会红。
  // vitest 的 cwd 是前端包根目录（`import.meta.url` 在这个环境里不是 file: 协议）。
  const source = readFileSync(
    resolve(process.cwd(), 'src/routes/games/trpg/RoomPage.tsx'), 'utf-8')

  it('两条待掷推送都走 appendOnce 且带上各自的键', () => {
    expect(source).toContain('dedupeKey: `check-request:${reqId}`')
    expect(source).toContain('dedupeKey: `san-check-request:${reqId}`')
    expect(source.match(/setMessages\(prev => appendOnce\(prev, \{/g) ?? []).toHaveLength(2)
  })

  it('重复到达不许把「正在掷」打回未掷', () => {
    // 无条件 setPendingCheck 会把 rolling:true 覆盖成 false —— 玩家已经点下去的
    // 掷骰按钮会跳回可点状态。断言写成完整的三元，反例（去掉 prev &&）装不下。
    expect(source).toContain("setPendingCheck(prev => (prev && prev.id === reqId ? prev : {")
    expect(source).toContain("setPendingCheck(prev => (prev && prev.id === reqId\n")
  })
})
