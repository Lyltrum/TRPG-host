import { expect, it } from 'vitest'
import { canReportRoll, isValidD100 } from './manual-roll'

it('1-100 才是一颗 d100 掷得出来的数', () => {
  expect(isValidD100('1')).toBe(true)
  expect(isValidD100('100')).toBe(true)
  expect(isValidD100('47')).toBe(true)
  for (const bad of ['0', '101', '999', '', '-1', '4.7', 'abc', ' 47']) {
    expect(isValidD100(bad), `「${bad}」不该被当成合法出目`).toBe(false)
  }
})

it('房间没开开关就不给入口', () => {
  expect(canReportRoll(false, 'skill')).toBe(false)
  expect(canReportRoll(true, 'skill')).toBe(true)
})

it('🔴 房间信息还没回来时按「关」处理', () => {
  // 宁可少给一个入口，也不要先显示出来再消失
  expect(canReportRoll(undefined, 'skill')).toBe(false)
})

it('🔴 理智检定不给报数——目标值玩家自己看不见', () => {
  expect(canReportRoll(true, 'san')).toBe(false)
})

it('🔴 RoomPage 真的用了这两个函数，不是自己又写了一遍判据', async () => {
  // 「加了函数没有消费方 = 没加」的守门人。这个仓库里判据被抄成第二份、
  // 两份慢慢分叉、而两头都不会变红，已经发生过好几次。
  const source = await import('./RoomPage.tsx?raw').then((m) => m.default as string)
  expect(source).toContain('isValidD100(manualRoll)')
  expect(source).toContain('canReportRollFor(roomInfo?.allowManualRolls')
  // 报数那条路要把值真的传下去
  expect(source).toContain('handleRollCheck(manualRollNumber)')
  // 理智检定那条路不许带 rollValue
  expect(source).toContain('rollSanCheck(playerId, { checkRequestId: pendingCheck.id })')
})
