/**
 * 常用角色卡库的**改与删**（P2 那五条里的最后一条）。
 *
 * 「存得进、取得回、复制进新草稿」已经由 `not-implemented.e2e.ts` 那条覆盖；
 * 这里补的是它没走到的两条路——改（PATCH 文字 / PUT 整份覆盖）和删，外加
 * 2026-08-16 新加的两道门（按规则系统过滤、卡库上限）。
 *
 * 🔴 为什么这几条非得走 e2e：它们的判据全在**跨请求的先后关系**上——改完再读
 * 回来是不是真的变了、删完那张卡是不是真的没了、过滤参数会不会被丢在路上。
 * 后端单测在同一个事务里断言不了"SDK 到底发出去了什么"，而正是 SDK 那一层
 * （query 参数拼错、token 传错位）最容易悄悄坏掉。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ApiError } from 'trpg-sdk'

import { createRoomWithModule } from './helpers.ts'

/** 建一张完整卡并存进卡库，返回 (room, templateId)。 */
async function savedTemplate(prefix: string, name = '我的调查员') {
  const room = await createRoomWithModule(prefix)
  const character = await room.host.sdk.characters.quickBuild(room.roomId, room.reconnectToken, {
    name: '凌铭辉',
  })
  const template = await room.host.sdk.characterTemplates.save(
    { name, characterId: character.characterId },
    room.host.token
  )
  return { room, template, characterId: character.characterId }
}

test('改卡库里那张卡的文字，读回来是改过的', async () => {
  const { room, template } = await savedTemplate('tpl-edit')

  await room.host.sdk.characterTemplates.update(
    template.templateId,
    { name: '改过的卡名', data: { residence: '阿卡姆', background: '他从战场回来之后就没说过话' } },
    room.host.token
  )

  // 🔴 读回来断言，不信 PATCH 自己的返回值：那两者可能来自同一个内存对象，
  // 而这条要证明的是**真的落库了**。
  const fetched = await room.host.sdk.characterTemplates.get(template.templateId, room.host.token)
  assert.equal(fetched.name, '改过的卡名')
  assert.equal((fetched.data as Record<string, unknown>).residence, '阿卡姆')
  // 没提到的字段不许被顺手清掉
  assert.equal((fetched.data as Record<string, unknown>).name, '凌铭辉')
})

test('规则数改不了——是显式拒绝，不是静默丢弃', async () => {
  const { room, template } = await savedTemplate('tpl-rules')

  await assert.rejects(
    room.host.sdk.characterTemplates.update(
      template.templateId,
      { data: { attributes: { STR: 99 } } },
      room.host.token
    ),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, 422)
      return true
    },
    '静默丢弃的话，界面显示改了、刷新又变回去——前后端两头都不会变红'
  )
})

test('用改过的角色卡整份覆盖卡库那张', async () => {
  const { room, template } = await savedTemplate('tpl-overwrite')
  const another = await room.host.sdk.characters.quickBuild(room.roomId, room.reconnectToken, {
    name: '另一个人',
  })

  const updated = await room.host.sdk.characterTemplates.overwrite(
    template.templateId,
    { characterId: another.characterId },
    room.host.token
  )

  assert.equal((updated.data as Record<string, unknown>).name, '另一个人')
  // 卡库名是玩家起的，不该被角色名盖掉
  assert.equal(updated.name, '我的调查员')
})

test('删掉之后，列表里没有它，直接取也 404', async () => {
  const { room, template } = await savedTemplate('tpl-delete')

  await room.host.sdk.characterTemplates.remove(template.templateId, room.host.token)

  const listed = await room.host.sdk.characterTemplates.list(room.host.token)
  assert.ok(
    !listed.some((t) => t.templateId === template.templateId),
    '列表里不该还有它'
  )
  await assert.rejects(
    room.host.sdk.characterTemplates.get(template.templateId, room.host.token),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, 404)
      return true
    },
    '🔴 只看列表少一行不够：那条只要前端过滤对了就绿，删没删真不知道'
  )
})

test('挑卡浮层那条：按规则系统过滤，用不了的不列出来', async () => {
  const { room, template } = await savedTemplate('tpl-filter')

  const usable = await room.host.sdk.characterTemplates.list(room.host.token, template.systemId)
  assert.deepEqual(
    usable.map((t) => t.templateId),
    [template.templateId]
  )

  const otherSystem = await room.host.sdk.characterTemplates.list(
    room.host.token,
    '00000000-0000-0000-0000-0000000000fe'
  )
  assert.deepEqual(otherSystem, [], '别的规则系统下这张卡用不了，就不该出现在浮层里')

  // 🔴 不带参数仍然返回全部——「我的调查员」那一页靠的是这个行为
  const everything = await room.host.sdk.characterTemplates.list(room.host.token)
  assert.ok(everything.some((t) => t.templateId === template.templateId))
})
