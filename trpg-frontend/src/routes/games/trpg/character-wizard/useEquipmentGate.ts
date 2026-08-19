import { useState } from 'react'
import type { Ruleset } from 'trpg-sdk'
import { checkEquipment } from '@/services/character/character-api'
import { useRoomStore } from '@/stores/room-store'
import type { RejectedEquipment } from '@/services/character/ruleset-api'
import type { WizardState } from './wizard-state'

/**
 * 装备那一关：**离开装备那一步时就审**，不等填完整张卡才拦回来。
 *
 * ## 🔴 为什么提前（真人反馈 2026-08-19）
 *
 * 「在这里进行提示感觉很生硬呀，应该在装备那个界面点击下一步就该有」。
 * 原来唯一的闸门在 `complete`，那是**第 8 步**，而装备栏在**第 7 步**——
 * 中间隔着一整屏背景故事，玩家一路填完才被打回来。
 *
 * **不是第二道闸门**：`complete` 那道照跑，判据、prompt、放行规则全部复用
 * 同一条路。两份判据迟早分叉，而分叉的方向一定是"预览说行、提交说不行"。
 *
 * ## 🔴 同一段文本不重复审
 *
 * 每次点下一步都打一次 LLM 要 3–5 秒。玩家在第 7 步和第 8 步之间来回翻是
 * 常事，而装备一个字没改时那次调用的结论必然一样——记住上次审过的文本，
 * 没变就直接放行。
 */
export function useEquipmentGate(state: WizardState, ruleset: Ruleset | null) {
  const [checking, setChecking] = useState(false)
  const [rejected, setRejected] = useState<RejectedEquipment[]>([])
  /** 上一次审过的装备原文 + 当时用的说明，两者都没变就不必再审。 */
  const [lastAudited, setLastAudited] = useState<string | null>(null)

  const creditRating = state.skillAlloc['credit-rating'] ?? null
  const occupationName =
    ruleset?.occupations.find((o) => o.id === state.occupationId)?.name ?? null

  /** 审一遍。返回 true = 可以往下走（通过、或这次判断没跑成）。 */
  const audit = async (notes: Record<string, string> = {}): Promise<boolean> => {
    const roomId = useRoomStore.getState().roomId
    const characterId = useRoomStore.getState().characterId
    const items = state.equipment
      .split(/[,，\n]/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (!roomId || !characterId || items.length === 0) return true

    // 🔴 指纹带上说明：玩家改了理由再点，必须重审，否则申辩那一步永远不生效。
    const fingerprint = JSON.stringify([items, notes])
    if (fingerprint === lastAudited) return rejected.length === 0

    setChecking(true)
    try {
      const result = await checkEquipment(roomId, characterId, {
        equipment: items,
        occupation: occupationName,
        age: state.age,
        residence: state.info.residence || null,
        birthplace: state.info.birthplace || null,
        creditRating,
        notes,
      })
      setLastAudited(fingerprint)
      // 🔴 `checked === false` 是**没判成**（没配 key / 超时），不是"全都合理"：
      // 放行，但不记指纹之外的任何结论——同后端那条「模型说不行和模型没说话
      // 是两回事」。
      const list = result.checked ? result.rejected : []
      setRejected(list)
      return list.length === 0
    } catch {
      // 审核这一步失败不该挡住建卡（同后端：把可用性押给第三方不叫严格）。
      setRejected([])
      return true
    } finally {
      setChecking(false)
    }
  }

  return { checking, rejected, setRejected, audit }
}
