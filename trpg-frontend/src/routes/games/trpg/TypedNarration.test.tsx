import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TypedNarration from './TypedNarration'

/** 逐字浮现（`exec/28`）。三条守的是三个不同的坑：
 *
 * 1. **历史不重打** —— `animate=false` 必须立刻全显。刷新页面后把整局叙事
 *    重新打一遍字是 `exec/26 #62` 明确点名要避免的。
 * 2. **不落后于到达** —— 我们的流式是按句的（一次 20–30 字），固定速度的
 *    打字机会越积越多，最后一句比实际到达晚好几秒。所以积压越多打得越快。
 * 3. **可跳过** —— 而且跳过之后**后续追加的段落也不再逐字**，否则玩家点了
 *    跳过，下一段又开始慢慢打。
 */

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.useFakeTimers()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.useRealTimers()
})

function render(node: React.ReactElement) {
  act(() => {
    root.render(node)
  })
}

/** 推进假时钟。
 *
 * 🔴 必须**分步**推进，不能一次 advance 到底：打字机是「effect 调度一个
 * timer → 回调里 setState → 重渲染 → effect 再调度下一个」的链条，而 effect
 * 只在 `act()` 结束时才 flush。一次性推完时钟只会走完**第一个** timer，
 * 后面的还没被调度出来，测出来永远只有一个字。 */
function runTyping(ms = 3000, step = 5) {
  for (let elapsed = 0; elapsed < ms; elapsed += step) {
    act(() => {
      vi.advanceTimersByTime(step)
    })
  }
}

describe('TypedNarration', () => {
  it('animate=false 立刻全显——历史消息不重新打一遍', () => {
    const text = '他推开门，屋里一片死寂。壁炉还残留着余温。'
    render(<TypedNarration text={text} animate={false} skipSignal={0} />)

    expect(container.textContent).toBe(text)
  })

  it('animate=true 从零开始逐字浮现', () => {
    const text = '他推开门，屋里一片死寂。'
    render(<TypedNarration text={text} animate skipSignal={0} />)

    expect(container.textContent).toBe('')

    runTyping(200)
    const partial = container.textContent ?? ''
    expect(partial.length).toBeGreaterThan(0)
    expect(partial.length).toBeLessThan(text.length)
    expect(text.startsWith(partial)).toBe(true)

    runTyping()
    expect(container.textContent).toBe(text)
  })

  it('🔴 积压很多时加速，不会落后于到达速度', () => {
    // 一次到达一大段（按句流式下这是常态），固定 30ms/字要 6 秒才打得完。
    const long = '这是一段很长的叙事。'.repeat(20) // 200 字
    render(<TypedNarration text={long} animate skipSignal={0} />)

    runTyping(1000)

    expect(container.textContent).toBe(long)
  })

  it('🔴 跳过之后，后续追加的段落也直接显示', () => {
    const first = '他推开门，屋里一片死寂。'
    render(<TypedNarration text={first} animate skipSignal={0} />)
    runTyping(100)
    expect(container.textContent?.length).toBeLessThan(first.length)

    // 玩家点了一下
    render(<TypedNarration text={first} animate skipSignal={1} />)
    expect(container.textContent).toBe(first)

    // 下一段 delta 追加上来——不能又开始慢慢打
    const appended = first + '壁炉还残留着余温。'
    render(<TypedNarration text={appended} animate skipSignal={1} />)
    expect(container.textContent).toBe(appended)
  })

  it('文本被权威全文校正成更短时，游标跟着收回来', () => {
    const long = '他推开门，屋里一片死寂。壁炉还残留着余温。'
    render(<TypedNarration text={long} animate skipSignal={0} />)
    runTyping()
    expect(container.textContent).toBe(long)

    const shorter = '他推开门，屋里一片死寂。'
    render(<TypedNarration text={shorter} animate skipSignal={0} />)
    expect(container.textContent).toBe(shorter)
  })
})
