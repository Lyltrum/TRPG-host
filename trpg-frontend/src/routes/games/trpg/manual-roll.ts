/**
 * 「我自己掷的」那颗骰（`exec/46` B5）。
 *
 * 判据抽出来单独放，是因为它有两条**只有前端能守**的规则，而 `RoomPage`
 * 本身太大、测起来只能靠整屏渲染：
 *
 * 1. **1–100 才是一颗 d100 掷得出来的数**（后端 DTO 会再挡一次，这里是即时反馈）
 * 2. **理智检定不给报数**——目标值玩家自己看不见（那是它的设计），报一个数却
 *    不知道自己在赌什么，比系统掷更差。后端也没接这一半。
 */

/** 输入框里那串字符算不算一个合法的 d100 出目。 */
export function isValidD100(raw: string): boolean {
  if (!/^\d{1,3}$/.test(raw)) return false
  const value = Number.parseInt(raw, 10)
  return value >= 1 && value <= 100
}

/** 这一次待掷检定给不给「我自己掷的」入口。 */
export function canReportRoll(
  allowManualRolls: boolean | undefined,
  checkKind: string | null | undefined
): boolean {
  // 🔴 `undefined`（房间信息还没回来）按**关**处理：宁可少给一个入口，
  //    也不要先显示出来再消失。
  if (!allowManualRolls) return false
  return checkKind !== 'san'
}
