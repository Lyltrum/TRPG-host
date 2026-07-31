import { beforeEach, describe, expect, it } from 'vitest'
import { useCharacterStore, type CompletedCharacter } from './character-store'

function card(name: string): CompletedCharacter {
  return {
    info: { name, playerName: '', age: '30', gender: '男', residence: '', birthplace: '', occupationId: null },
    attr: { STR: 50 },
    skillAlloc: {},
    skillFinalValues: {},
    equipment: '',
    background: '',
    notes: '',
    derived: { hp: 10, san: 50, mp: 10, db: '0', move: 8 },
  }
}

describe('character-store 的读取隔离', () => {
  beforeEach(() => {
    useCharacterStore.getState().clear()
  })

  it('房间对不上就当作没建过卡', () => {
    useCharacterStore.getState().setCharacter(card('甲'), 'room-1', 'p1')
    expect(useCharacterStore.getState().getForRoom('room-2', 'p1')).toBeNull()
  })

  it('🔴 同一房间里玩家对不上也读不出来', () => {
    // 同一浏览器两个标签页进同一个房间：后建完卡的会覆盖先建的那份缓存。
    // 不按玩家核对的话，先建卡那个标签页会把队友的卡当成自己的显示出来。
    useCharacterStore.getState().setCharacter(card('甲'), 'room-1', 'p1')
    expect(useCharacterStore.getState().getForRoom('room-1', 'p2')).toBeNull()
    expect(useCharacterStore.getState().getForRoom('room-1', 'p1')?.info.name).toBe('甲')
  })

  it('老缓存没有 playerId 时只按房间判断（不让升级前建的卡突然读不出来）', () => {
    useCharacterStore.getState().setCharacter(card('甲'), 'room-1', null)
    expect(useCharacterStore.getState().getForRoom('room-1', 'p1')?.info.name).toBe('甲')
  })
})
