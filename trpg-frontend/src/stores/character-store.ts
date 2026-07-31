import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type { Attributes, BackgroundDetail, InvestigatorInfo } from '@/data/character-model'

export interface CompletedCharacter {
  info: InvestigatorInfo
  attr: Attributes
  skillAlloc: Record<string, number>
  // 全部技能的最终值（skillId -> 已经算好的 base+分配，来自建卡完成时后端
  // previewCharacter 返回的权威计算结果），人物卡准备页/游戏内技能面板直接
  // 渲染这份数据，不用再拿 skillAlloc 现场重算（issue #84 S3：规则计算全部
  // 交给后端，前端只存/渲染结果）。
  skillFinalValues: Record<string, number>
  equipment: string
  background: string
  notes: string
  derived: { hp: number; san: number; mp: number; db: string; move: number }
  // 结构化背景故事（character-build-migration），可选——旧缓存/没填过的
  // 角色卡没有这个字段，读取处要能容忍 undefined。
  backgroundDetail?: BackgroundDetail
}

interface CharacterState {
  character: CompletedCharacter | null
  // 这份角色数据属于哪个房间的哪个玩家——见下面的 getForRoom，读取时两个都要
  // 核对，不能假设 localStorage 里存的就是当前这个人的角色。
  roomId: string | null
  playerId: string | null
  setCharacter: (c: CompletedCharacter, roomId: string, playerId: string | null) => void
  getForRoom: (roomId: string, playerId: string | null) => CompletedCharacter | null
  clear: () => void
}

// 🔴 这份缓存**不是权威源**。角色卡的权威源是后端（issue #96 起有读接口，
// `services/character/character-api.ts::fetchCharacter`）；准备页与聊天室都
// 已改成进房拉一次、HP/SAN 变动后重拉。这里留下来只剩两个用途：
//   ① 拉回来之前的**首屏占位**，避免闪一下空白；
//   ② **建卡向导编辑已有角色卡**时恢复 `skillAlloc`——后端只存技能终值，
//      不存"玩家分配了多少点"，这份分配值目前只有本地有。
// 别再往这里加"局内状态"（HP/SAN 之类）：那类数据会被服务端改写，本地镜像
// 不随权威源重建就必然偏（掉线期间错过广播就再也补不回来）。
//
// 写在 localStorage 而不是 sessionStorage：用途②要跨标签页/跨会话活着
// （从「我的游戏」回到一局早前建过卡的房间还要能编辑）。
//
// ★ 只存一份角色曾经导致两次真实 bug：不按房间区分 → 加入另一个房间会展示
// 上一个房间的角色卡（PR #67 review）；不按玩家区分 → **同一浏览器两个标签页
// 进同一个房间**，后建完卡的会覆盖先建的，两边面板显示同一张卡。所以 roomId
// 和 playerId 两个都要核对，任一对不上就当作没建过卡。
export const useCharacterStore = create<CharacterState>()(
  persist(
    (set, get) => ({
      character: null,
      roomId: null,
      playerId: null,
      setCharacter: (character, roomId, playerId) => set({ character, roomId, playerId }),
      getForRoom: (roomId, playerId) => {
        const state = get()
        if (state.roomId !== roomId) return null
        // playerId 存了就必须对得上。老缓存没有这个字段（null），此时只按
        // roomId 判断——退回改动前的行为，不至于让升级前建的卡突然读不出来。
        if (state.playerId !== null && state.playerId !== playerId) return null
        return state.character
      },
      clear: () => set({ character: null, roomId: null, playerId: null }),
    }),
    { name: 'aidm-character', storage: createJSONStorage(() => localStorage) }
  )
)
