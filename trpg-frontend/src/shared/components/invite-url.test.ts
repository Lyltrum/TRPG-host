import { describe, expect, it } from 'vitest'

import { inviteUrlFor } from './InviteSheet'

describe('邀请链接', () => {
  it('🔴 只带房间码，域名用当前 host', () => {
    // 局域网 IP 会变、以后可能上公网——链接里写死任何地址，换一次网络就得
    // 改一次代码。同族于「路径别数层数，找锚点」。
    expect(inviteUrlFor('WSWLYK')).toBe(`${window.location.origin}/join/WSWLYK`)
  })

  it('房间码原样拼进路径，不做任何转换', () => {
    // 落地页自己会 toUpperCase，这里多做一次转换就是两处规则
    expect(inviteUrlFor('abc123')).toBe(`${window.location.origin}/join/abc123`)
  })
})
