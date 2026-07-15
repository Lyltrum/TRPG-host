import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type { Attributes, InvestigatorInfo } from '@/data/character-model'

export interface CompletedCharacter {
  info: InvestigatorInfo
  attr: Attributes
  skillAlloc: Record<string, number>
  equipment: string
  background: string
  notes: string
  derived: { hp: number; san: number; mp: number; db: string; move: number }
}

interface CharacterState {
  character: CompletedCharacter | null
  setCharacter: (c: CompletedCharacter) => void
  clear: () => void
}

// ★ 之前只存在内存里，换个浏览器 tab（比如从「我的游戏」继续一局早前建过卡
// 的房间）角色数据就丢了，聊天室的"角色卡/技能"面板会看起来是空的——不是没
// 建过卡，是本地状态没了。持久化到 localStorage 解决同浏览器下的这类场景
// （换浏览器/清缓存仍然会丢，因为这份数据从来没真的存过后端，见 mock-db 里
// MockCharacter 只存了 draft/complete 状态、没存真实建卡内容）。
export const useCharacterStore = create<CharacterState>()(
  persist(
    (set) => ({
      character: null,
      setCharacter: (character) => set({ character }),
      clear: () => set({ character: null }),
    }),
    { name: 'aidm-character', storage: createJSONStorage(() => localStorage) }
  )
)
