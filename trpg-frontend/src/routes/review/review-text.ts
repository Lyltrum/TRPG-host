import type { RoomPreview, RoomSummary } from '@/services/room'

/**
 * 把一局的复盘拼成一段**纯文本**，供玩家复制出去发到群里（`exec/46` B9）。
 *
 * ## 为什么是纯文本而不是文件下载
 *
 * 这个产品的定位是**线下聚会、手机为主**。手机浏览器的文件下载多半落进一个
 * 玩家找不到的目录，而"复制 → 粘进群里"是他本来就会做的动作。
 *
 * ## 空段整段省略
 *
 * `null` 与 `[]` 含义不同（没结束 / 全查到了），但对这段文本来说都是"没有这
 * 一节"——**唯独不能把它渲染成一个空标题**，那是最坏的一种表达（`exec/29` 里
 * `NO_ENDINGS_NOTICE` 那条判据）。
 */
export function buildReviewText(room: RoomPreview, summary: RoomSummary | null): string {
  const parts: string[] = []
  parts.push(`《${room.moduleTitle || '未知模组'}》· ${room.roomName}`)

  const players = room.players.map((p) => p.nickname).join('、')
  if (players) parts.push(`调查员：${players}`)

  if (summary?.highlights?.length) {
    parts.push('', '【这一局】', ...summary.highlights.map((l) => `· ${l}`))
  }
  if (summary?.summaryText) {
    parts.push('', '【案件回顾】', summary.summaryText)
  }
  if (summary?.missedTruths?.length) {
    parts.push('', '【你们没查到的】', ...summary.missedTruths.map((l) => `· ${l}`))
  }
  return parts.join('\n')
}

/**
 * 复制到剪贴板。**返回是否成功——调用方必须处理失败**。
 *
 * 🔴 **`navigator.clipboard` 在非安全上下文里根本不存在**，而这个项目的主场
 * 恰恰是局域网 `http://<内网IP>:9877`（见项目 CLAUDE.md 的「局域网开局」）。
 * 在那里它是 `undefined`，不是抛异常——直接调会 TypeError。
 *
 * 所以这里**先问在不在**，不在就如实返回 false，让调用方把文本摊开给用户自己
 * 长按复制。**不做静默兜底**：假装复制成功是这一屏最容易犯的错。
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard?.writeText) return false
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // 有剪贴板 API 但被权限拒绝（用户没授权、iframe 里）也走同一条降级路。
    return false
  }
}
