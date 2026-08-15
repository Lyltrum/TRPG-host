import { describe, expect, it } from 'vitest'

import {
  TIMELINE_EVENT_RENDERERS,
  checkResultToEventPayload,
  formatCheckLine,
} from './timeline-events'

describe('时间线事件表（#82：刷新之后掷骰卡片就没了）', () => {
  it('掷骰事件在表里 —— 这正是回放此前漏掉的那一种', () => {
    // 🔴 回归的核心：`keeper.check` 事件一直存在库里（实测那一局有 21 条），
    // 但重连回放只认 narration.push / action.submit 两种，于是刷新即失。
    expect(TIMELINE_EVENT_RENDERERS['keeper.check']).toBeDefined()
    expect(TIMELINE_EVENT_RENDERERS['keeper.luck_spend']).toBeDefined()
  })

  it('落库形状能渲染成一条掷骰消息', () => {
    const message = TIMELINE_EVENT_RENDERERS['keeper.check']({
      player: '凌铭辉',
      skill: '侦察',
      rolled: 51,
      target: 51,
      level: '成功',
    })
    expect(message).toEqual({
      type: 'dice',
      sender: '凌铭辉',
      content: '侦察 · 51/51 · 成功',
    })
  })

  it('缺关键数据时不产生半条消息', () => {
    expect(TIMELINE_EVENT_RENDERERS['keeper.check']({ player: '凌铭辉' })).toBeNull()
  })

  it('对抗检定把胜负一起显示', () => {
    const line = formatCheckLine({
      skill: '敏捷',
      rolled: 67,
      target: 65,
      level: '成功',
      opposed: { opponent: '看护', rolled: 9, target: 50, level: '极难成功', won: false },
    })
    expect(line).toContain('vs 看护 9/50')
    expect(line).toContain('负')
  })

  it('花过幸运时两个出目都显示', () => {
    // 只给原始出目的话卡片上会是「7/5 · 成功」——7 大于 5 却成功，说不通。
    const line = formatCheckLine({
      skill: '话术',
      rolled: 7,
      target: 5,
      level: '成功',
      effective_rolled: 5,
      luck_spent: 2,
    })
    expect(line).toContain('7→5/5')
    expect(line).toContain('幸运 -2')
  })

  it('没花幸运时不出现幸运字样（退化保证）', () => {
    const line = formatCheckLine({ skill: '侦察', rolled: 83, target: 51, level: '失败' })
    expect(line).toBe('侦察 · 83/51 · 失败')
  })
})

describe('实时与回放走同一份文案', () => {
  it('WS payload 适配之后渲染出跟落库形状一模一样的那行', () => {
    // 🔴 这条断言就是 #82 的修法本身：两条路各写各的文案，正是它的成因。
    const fromLive = formatCheckLine(
      checkResultToEventPayload({
        skill: '侦察',
        rollValue: 51,
        targetValue: 51,
        result: '成功',
      }),
    )
    const fromReplay = formatCheckLine({
      skill: '侦察',
      rolled: 51,
      target: 51,
      level: '成功',
    })
    expect(fromLive).toBe(fromReplay)
  })

  it('对抗与幸运两种情形也一致', () => {
    const ws = {
      skill: '力量',
      rollValue: 27,
      targetValue: 60,
      result: '困难成功',
      effectiveRollValue: 60,
      luckSpent: 5,
      opposedOpponent: '米-戈',
      opposedRollValue: 93,
      opposedTargetValue: 75,
      opposedWon: true,
    }
    expect(formatCheckLine(checkResultToEventPayload(ws))).toBe(
      formatCheckLine({
        skill: '力量',
        rolled: 27,
        target: 60,
        level: '困难成功',
        effective_rolled: 60,
        luck_spent: 5,
        opposed: { opponent: '米-戈', rolled: 93, target: 75, won: true },
      }),
    )
  })
})

describe('NPC 掷的骰不上时间线', () => {
  // 🔴 08-14 实测：叙事写「州警扣下扳机」，卡片却是「凌铭辉 · 射击」。
  // 后端已改成把 NPC 的检定记在 `npc` 键上；前端这一半的判据是**两条路一致**
  // ——实时推不出 NPC 卡片（`check.result` 的 playerId 必填），那回放也不许
  // 凭空多出一张，否则又是 #82「同一件事两条路只有一条认得」。

  it('带 npc 的检定不渲染成卡片', () => {
    const render = TIMELINE_EVENT_RENDERERS['keeper.check']
    expect(
      render({ npc: '爬行者 #1', skill: '爪击', rolled: 55, target: 70, level: '成功' }),
    ).toBeNull()
  })

  it('玩家的检定照常渲染', () => {
    const render = TIMELINE_EVENT_RENDERERS['keeper.check']
    const message = render({ player: '凌铭辉', skill: '侦察', rolled: 51, target: 51, level: '成功' })
    expect(message).not.toBeNull()
    expect(message?.sender).toBe('凌铭辉')
  })
})
