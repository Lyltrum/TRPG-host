/**
 * 钉住「目前还是桩」的那些接口。
 *
 * 这类用例的作用不是保护现状，而是**当它们变红时提醒我们去接**：队友把
 * 掷骰检定 / 模组导入 / 复盘摘要真正实现之后，这里会失败，我们就知道该把
 * 对应的客户端接入补上，而不是等到某天有人手工发现"后端早就能用了"。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ApiError } from 'trpg-sdk'

import { createRoomWithModule } from './helpers.ts'

/**
 * 断言的是**具体的 501 / NOT_IMPLEMENTED 契约**，不是「这个调用会失败」。
 *
 * 只要求 Promise 拒绝的话，路由被删成 404、后端内部报错变成 500、甚至响应解析
 * 失败，这里都会继续绿——真正的回归会被误判成「仍是桩」，正好和这两条用例想
 * 提供的信号相反。后端刻意用 501 而不是 500，就是为了让客户端能区分「服务器出
 * bug 了」和「这个功能确实还没做」（见 `app/core/errors.py::not_implemented`），
 * 那这里就该把这个区分钉住。
 */
function assertNotImplemented(hint: string) {
  return (error: unknown) => {
    assert.ok(error instanceof ApiError, `${hint}（期望 ApiError，实际 ${String(error)}）`)
    assert.equal(error.code, 'NOT_IMPLEMENTED', hint)
    assert.equal(error.status, 501, hint)
    return true
  }
}

test('🔴 复盘摘要已经实现：数字一定有，那段回顾没 key 时是 null', async () => {
  // 这条原本是 NOT_IMPLEMENTED 的守卫，它自己写着「变红说明复盘已经实现，
  // 该去看客户端要不要接」——2026-08-12 正是那一天。
  const room = await createRoomWithModule('recap')
  await room.host.sdk.rooms.disband(room.roomId, room.reconnectToken)

  const summary = await room.host.sdk.rooms.getSummary(room.roomId, room.reconnectToken)

  assert.equal(summary.roomId, room.roomId)
  assert.ok(Array.isArray(summary.highlights), '数字那一半是代码算的，一定在')
  assert.ok(
    summary.highlights!.some((line) => line.includes('这一局跑了')),
    '结束了的房间至少要有时长'
  )
  // 🔴 e2e 不配 DEEPSEEK_API_KEY ⇒ 那段回顾如实为 null，**不伪造**。
  assert.equal(summary.summaryText, null)
})

test('🔴 常用角色卡库已经实现：存得进、取得回、复制进新草稿', async () => {
  // 这条原本也是 NOT_IMPLEMENTED 的守卫，写着「变红说明已经实现，该去接
  // 客户端」——2026-08-13 正是那一天：service 层四个函数此前全是
  // `raise not_implemented`，前端一次都没调过。
  const room = await createRoomWithModule('tpl')
  const character = await room.host.sdk.characters.quickBuild(
    room.roomId,
    room.reconnectToken,
    { name: '凌铭辉' }
  )

  const saved = await room.host.sdk.characterTemplates.save(
    { name: '我的记者', characterId: character.characterId },
    room.host.token
  )
  assert.equal(saved.name, '我的记者')

  const listed = await room.host.sdk.characterTemplates.list(room.host.token)
  assert.deepEqual(
    listed.map((t) => t.templateId),
    [saved.templateId]
  )

  // 第二局：拿常用卡开草稿。**复制不是引用**，而且仍是 draft（复用不等于跳过校验）
  const second = await createRoomWithModule('tpl2', room.host)
  const draft = await second.host.sdk.characters.createDraft(
    second.roomId,
    second.reconnectToken,
    saved.templateId
  )
  assert.equal(draft.status, 'draft')
})

test('复盘事件流已经是真实现（不是桩）', async () => {
  // 跟上面两条相反：replay 是真的，客户端却从没调用过——属于「后端能力就绪
  // 但没接」，不是功能缺失。
  const room = await createRoomWithModule('replay')
  const events = await room.host.sdk.rooms.getReplay(room.roomId, room.reconnectToken)
  assert.ok(Array.isArray(events), 'replay 应该返回事件数组')
})
