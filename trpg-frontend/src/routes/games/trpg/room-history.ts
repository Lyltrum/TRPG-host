/**
 * 进房时把 `GET /replay` 补来的历史与**挂载后 WS 已经送到**的实时消息合成一条
 * 时间线。
 *
 * ## 🔴 为什么要有这个函数（真人实测 2026-08-11，多人局）
 *
 * 原来那段 boot 逻辑最后一步是 `setMessages(boot)` —— **整体覆盖**，而 WS 那条
 * 路同时在 `setMessages(prev => [...prev, x])` 往里追加。开场叙事要十几秒才生成
 * 得出来，于是：
 *
 * - **房主**点了开始就进房，boot 的 3.2 秒轮询早早结束，叙事随后到达 → 正常；
 * - **另一个玩家**是靠 3 秒一次的房间状态轮询才跳进对局页的，**进得晚**，他那
 *   3.2 秒窗口正好压在叙事到达的时刻上 → WS 先把叙事追进 messages，boot 随后
 *   一覆盖就**抹掉了**。
 *
 * 而且是**永久性的**：那条 eventId 已经进了 `seenEventKeysRef`，再也不会被重新
 * 加回来；`typing` 也没人来关，「守秘人正在思考」就一直亮着。刷新能好，是因为
 * 那时 replay 里已经有叙事、去重表又是新的。
 *
 * 同族于 `exec/19 #34`（覆盖 + 去重 = 永久丢失），只是上次的入口是重订阅。
 *
 * 抽成纯函数是为了**它能有一条测试**——组件里那段异步 effect 的时序在单测里
 * 摆不出来，而这条规则本身是纯的。
 */
/**
 * `history` 是 replay 补的（全是历史），`live` 是挂载后 WS 送到的。
 *
 * 历史在前、实时在后：`live` 里的每一条都发生在 boot 开始轮询之后。
 * **不在这里去重**——调用方的 `dedupe` 闸门已经保证了同一个 eventId 只会进其中
 * 一边（WS 先收下的那条，boot 循环里会被跳过）。在这里再去一次重反而会掩盖
 * 闸门本身的失效。
 */
export function mergeRoomHistory<T>(history: T[], live: T[]): T[] {
  return [...history, ...live]
}

/**
 * 「守秘人正在思考」该不该亮。
 *
 * 🔴 判据是**合并之后有没有叙事**，不是 boot 自己那个数组里有没有——后者看不见
 * WS 已经送到的那条，正是上面那个 bug 的第二个症状（消息被抹掉的同时指示器被
 * 点亮，然后永远没人关）。
 */
export function shouldShowThinking(merged: { type: string }[]): boolean {
  return !merged.some((m) => m.type === 'narr')
}

/**
 * 追加一条消息，但**同一个 `dedupeKey` 只进一次**。
 *
 * ## 🔴 为什么需要它（三人真机 2026-08-19）
 *
 * 多人局里两个人同时被要求掷骰，**先掷完的那个人走结算**时，pending 守卫会把
 * 还没掷的人那张卡**重发一遍**（`exec/23 #76` 的设计行为：提醒球还在你手上）。
 * 于是后掷的那个人收到两条一模一样的 `check.request`，聊天区多出一句重复的
 * 「守秘人请求你进行侦察检定」。
 *
 * **单人局在结构上撞不到**——那里不存在"另一个人正在掷"。
 *
 * 同一个 handler 里 `action.broadcast` 早就按 `messageId` 去过重了，检定这一半
 * 一直没有：**一份数据有几个出口，规则就要落几处。**
 *
 * 不带 `dedupeKey` 的照常追加——历史重建、重连补发都走那条路，行为不变。
 */
export function appendOnce<T extends { dedupeKey?: string }>(list: T[], item: T): T[] {
  if (item.dedupeKey && list.some(m => m.dedupeKey === item.dedupeKey)) {
    return list
  }
  return [...list, item]
}
