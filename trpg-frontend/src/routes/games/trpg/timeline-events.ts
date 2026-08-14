/**
 * 「事件 → 时间线上的一条消息」——**实时通道与重连回放共用的那一张表**。
 *
 * ## 🔴 为什么必须共用（真人实测 2026-08-14，问题 #82）
 *
 * 玩家刷新页面之后，**掷骰鉴定的卡片全没了**，叙事和自己的发言都还在。
 *
 * 查下来事件是**存了库**的（那一局 `keeper.check` 21 条、`keeper.luck_spend`
 * 2 条，技能/掷值/目标值/成功等级一样不缺）。坏的是重连回放：那个循环只认
 * **两种**事件（`narration.push` 与 `action.submit`），两个 `else if`，没有第三个。
 * 掷骰卡片只活在实时 WS 那一拍，刷新即失。
 *
 * 这是项目里那条老判据的现场：**同一件事有两个实现，功能写在其中一个里，
 * 换一条路进来就悄悄没了。**
 *
 * ## 修法：不是加第三个 `else if`
 *
 * 加分支能修好这一条，但下次再新增一种"要显示在时间线上的事件"还会再漏一次
 * ——那正是「逐个列出的地方，加一项就漏一项」。所以把映射抽成**一张表**，
 * 两条路都遍历它。新增一种事件 = 这张表加一行，两条路同时生效。
 *
 * ## 归一化到「落库形状」
 *
 * 两条路拿到的 payload 形状本来就不同：实时是 WS DTO（camelCase、
 * `rollValue`/`targetValue`），落库是事件 payload（`rolled`/`target`/`level`）。
 * 表以**落库形状**为准，实时侧先适配再进来——因为落库的那份是历史真相，
 * 回放只能读到它，而实时那份可以在发出前塑形。
 */

/** 时间线上的一条消息（与 RoomPage 的 messages 同构）。 */
export type TimelineMessage = {
  type: 'narr' | 'player' | 'dice' | 'system'
  sender?: string
  content: string
  time: string
  isSelf?: boolean
}

/** 一次检定的落库形状（`keeper.check` 的 payload）。 */
export type CheckEventPayload = {
  player?: string
  skill?: string
  rolled?: number
  target?: number
  level?: string
  /** 幸运补正之后的有效出目（2026-08-14 加）。 */
  effective_rolled?: number | null
  luck_spent?: number | null
  opposed?: {
    opponent?: string
    rolled?: number
    target?: number
    level?: string
    won?: boolean
  } | null
}

/**
 * 掷骰卡片的正文。**只有这一份**，实时与回放都调它。
 *
 * 花过幸运时两个出目都要显示：只给原始出目的话卡片上会是「7/5 · 成功」——
 * 7 大于 5 却成功，玩家没法自洽（同一局实测出来的另一条）。
 */
export function formatCheckLine(payload: CheckEventPayload): string {
  const target = payload.target ?? '?'
  const roll =
    payload.effective_rolled != null
      ? `${payload.rolled}→${payload.effective_rolled}/${target}（幸运 -${payload.luck_spent}）`
      : `${payload.rolled}/${target}`
  const opposed = payload.opposed?.opponent
    ? ` vs ${payload.opposed.opponent} ${payload.opposed.rolled}/${payload.opposed.target} · ${
        payload.opposed.won ? '胜' : '负'
      }`
    : ''
  return `${payload.skill ?? '检定'} · ${roll} · ${payload.level ?? ''}${opposed}`
}

/** 一次幸运消费的落库形状（`keeper.luck_spend` 的 payload）。 */
export type LuckSpendEventPayload = {
  player?: string
  cost?: number
  luck?: number
}

export function formatLuckSpendLine(payload: LuckSpendEventPayload): string {
  return `${payload.player ?? '调查员'} 消耗 ${payload.cost} 点幸运（剩 ${payload.luck}）`
}

/**
 * 事件类型 → 这条事件在时间线上长什么样。返回 `null` = 这类事件不上时间线。
 *
 * 🔴 **加一种要显示的事件就在这里加一行**，别回到两条路各写各的。
 */
export const TIMELINE_EVENT_RENDERERS: Record<
  string,
  (payload: Record<string, unknown>) => Omit<TimelineMessage, 'time'> | null
> = {
  'keeper.check': (payload) => {
    const check = payload as CheckEventPayload
    if (check.rolled == null) return null
    return { type: 'dice', sender: check.player, content: formatCheckLine(check) }
  },
  'keeper.luck_spend': (payload) => {
    const spend = payload as LuckSpendEventPayload
    if (spend.cost == null) return null
    return { type: 'system', content: formatLuckSpendLine(spend) }
  },
}

/**
 * 实时 `check.result` 的 WS payload → 落库形状。
 *
 * 存在的理由就是让实时那条路走进上面同一个 `formatCheckLine`：两边各写一份
 * 文案，正是 #82 的成因。
 */
export function checkResultToEventPayload(ws: {
  skill?: string
  rollValue?: number
  targetValue?: number | null
  result?: string
  effectiveRollValue?: number | null
  luckSpent?: number | null
  opposedOpponent?: string | null
  opposedRollValue?: number | null
  opposedTargetValue?: number | null
  opposedWon?: boolean | null
}): CheckEventPayload {
  return {
    skill: ws.skill,
    rolled: ws.rollValue,
    target: ws.targetValue ?? undefined,
    level: ws.result,
    effective_rolled: ws.effectiveRollValue,
    luck_spent: ws.luckSpent,
    opposed: ws.opposedOpponent
      ? {
          opponent: ws.opposedOpponent,
          rolled: ws.opposedRollValue ?? undefined,
          target: ws.opposedTargetValue ?? undefined,
          won: ws.opposedWon ?? undefined,
        }
      : null,
  }
}
