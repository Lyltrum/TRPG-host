import { useEffect, useRef, useState } from 'react'

/** 守秘人叙事的逐字浮现（`exec/28`）。
 *
 * 🔴 **它只是渲染层。** 气泡的 `text` 永远是真值——打字机只决定"显示到第几个
 * 字"，不参与接收、拼接、去重。哪天要把它关掉，删掉这个组件的调用即可，数据
 * 流一行不用动。
 *
 * ## 为什么自适应的方向跟别家反着
 *
 * 按 token 流式的产品（比如 Claude 的界面）用打字机是为了**降噪**：输入碎到
 * 几个字符一次，裸渲染会抖。我们的流式是**按句**的——一次 20–30 字、间隔
 * 0.5–1 秒，没有碎片要平滑，真正的风险是**落后**：这一句还没打完，下一句已经
 * 到了，越积越多，最后一句可能比实际到达晚好几秒。
 *
 * 所以速度跟着**积压量**走：欠得越多打得越快，欠得太多直接整块吐出去。宁可
 * 偶尔失去逐字感，也不能让玩家等一段已经算好的文字。
 */

/** 积压字数 → 每字间隔（ms）。最后一档是"别打了，直接给"。 */
function delayFor(pending: number): number {
  if (pending > 90) return 0
  if (pending > 45) return 8
  if (pending > 18) return 18
  return 30
}

/** 一次推进几个字。积压很多时成块吐，避免 setState 打成一片。 */
function stepFor(pending: number): number {
  if (pending > 90) return pending
  if (pending > 45) return 3
  return 1
}

export default function TypedNarration({
  text,
  animate,
  skipSignal,
  onReveal,
}: {
  text: string
  /** false = 直接全显。历史消息、replay 补的内容都走这条——🔴 刷新页面后把
   *  整局叙事重打一遍字，玩家会疯（`exec/26 #62` 第一条要求）。 */
  animate: boolean
  /** 递增即表示"玩家要求跳过"。跳过之后这条消息不再打字，后续追加也直接显示。 */
  skipSignal: number
  /** 每次显示长度变化时回调，供外层跟着滚到底。 */
  onReveal?: () => void
}) {
  const [revealed, setRevealed] = useState(animate ? 0 : text.length)
  const skippedRef = useRef(!animate)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 跳过：一次点击之后这条就永久全显，后面再追加的 delta 也不再逐字
  const firstSkipRef = useRef(skipSignal)
  useEffect(() => {
    if (skipSignal !== firstSkipRef.current) skippedRef.current = true
  }, [skipSignal])

  useEffect(() => {
    if (skippedRef.current) {
      setRevealed(text.length)
      return
    }
    if (revealed >= text.length) return
    const pending = text.length - revealed
    timerRef.current = setTimeout(() => {
      setRevealed((r) => Math.min(text.length, r + stepFor(text.length - r)))
      onReveal?.()
    }, delayFor(pending))
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [revealed, text, skipSignal, onReveal])

  // 文本变短（narration.push 校正成了更短的权威全文）时把游标收回来，
  // 否则 slice 会停在旧长度上、显示不出后续变化。
  useEffect(() => {
    if (revealed > text.length) setRevealed(text.length)
  }, [text, revealed])

  return <>{text.slice(0, revealed)}</>
}
