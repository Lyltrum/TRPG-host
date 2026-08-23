import { expect, it } from 'vitest'
import { buildReviewText, copyText } from './review-text'
import type { RoomPreview, RoomSummary } from '@/services/room'

const room = {
  roomId: 'r1',
  roomCode: 'ABC123',
  roomName: '周五那局',
  moduleTitle: '林中屋',
  players: [
    { playerId: 'p1', nickname: '小林', isHost: true },
    { playerId: 'p2', nickname: '阿罗', isHost: false },
  ],
} as unknown as RoomPreview

function summary(over: Partial<RoomSummary>): RoomSummary {
  return { highlights: [], missedTruths: null, summaryText: null, ...over } as unknown as RoomSummary
}

it('把三节都拼进去', () => {
  const text = buildReviewText(
    room,
    summary({
      highlights: ['掷了 17 次骰'],
      summaryText: '他们最终烧掉了那栋木屋。',
      missedTruths: ['地窖底下还有一具尸体'],
    })
  )
  expect(text).toContain('《林中屋》· 周五那局')
  expect(text).toContain('调查员：小林、阿罗')
  expect(text).toContain('【这一局】')
  expect(text).toContain('· 掷了 17 次骰')
  expect(text).toContain('【案件回顾】')
  expect(text).toContain('他们最终烧掉了那栋木屋。')
  expect(text).toContain('【你们没查到的】')
})

it('空的那几节整段省略，不留空标题', () => {
  const text = buildReviewText(room, summary({}))
  expect(text).not.toContain('【这一局】')
  expect(text).not.toContain('【案件回顾】')
  expect(text).not.toContain('【你们没查到的】')
  // 但房间那一行永远在——否则复制出去是一段没头没尾的话
  expect(text).toContain('《林中屋》· 周五那局')
})

it('还没生成摘要时也拼得出东西', () => {
  expect(buildReviewText(room, null)).toContain('周五那局')
})

it('🔴 没有 clipboard API 时如实返回 false，不假装成功', async () => {
  // 局域网 http://<内网IP>:9877 就是这个情形——navigator.clipboard 是 undefined
  const original = navigator.clipboard
  Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
  expect(await copyText('x')).toBe(false)
  Object.defineProperty(navigator, 'clipboard', { value: original, configurable: true })
})

it('被权限拒绝时也返回 false', async () => {
  const original = navigator.clipboard
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: () => Promise.reject(new Error('denied')) },
    configurable: true,
  })
  expect(await copyText('x')).toBe(false)
  Object.defineProperty(navigator, 'clipboard', { value: original, configurable: true })
})

it('正常情况下返回 true 且真的写了', async () => {
  const written: string[] = []
  const original = navigator.clipboard
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: (t: string) => { written.push(t); return Promise.resolve() } },
    configurable: true,
  })
  expect(await copyText('复盘正文')).toBe(true)
  expect(written).toEqual(['复盘正文'])
  Object.defineProperty(navigator, 'clipboard', { value: original, configurable: true })
})
