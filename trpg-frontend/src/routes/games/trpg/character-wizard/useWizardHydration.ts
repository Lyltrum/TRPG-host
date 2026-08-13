import { useEffect } from 'react'
import type { Ruleset } from 'trpg-sdk'
import type { GenerationMethod } from '@/data/character-model'
import { emptyBackgroundDetail } from '@/data/character-model'
import { useRoomStore } from '@/stores/room-store'
import { fetchCharacter } from '@/services/character/character-api'
import { previewCharacter } from '@/services/character/ruleset-api'
import { DEFAULT_AGE, type WizardAction, type WizardHydratePatch } from './wizard-state'

/**
 * 从后端读回已保存的角色卡（issue #96：后端是角色卡的唯一事实来源，本地
 * 缓存只是加速用的，不能当权威源）。
 *
 * 时序是关键（§11 风险 5，现状踩过的真实教训）：技能点反推必须走后端的
 * 权威预览（ruleset 里的 base 有公式型，前端把公式串当 0 处理会重复叠加
 * base），所以要**先算后填**——先把属性发给 preview 算出 skillView.allocated，
 * 拿到结果后再一次性 dispatch 进表单；preview 失败就整体不水合，退回空白
 * 向导，不留半截水合的状态。
 */
export function useWizardHydration(
  ruleset: Ruleset | null,
  dispatch: (action: WizardAction) => void,
  // 🔴 characterId 要当依赖传进来，不能只在 effect 里 `getState()` 读：那样它
  // 变了不会重新水合。「用常用卡建卡」正是在向导开着的时候换掉 characterId 的
  // （2026-08-13），少了这个依赖，模板数据就永远填不进表单。
  characterId: string | null,
) {
  useEffect(() => {
    const roomId = useRoomStore.getState().roomId
    if (!roomId || !characterId || !ruleset) return

    let cancelled = false
    fetchCharacter(roomId, characterId)
      .then(async (saved) => {
        if (cancelled || !saved.attributes || Object.keys(saved.attributes).length === 0) return
        const savedAttrs = saved.attributes
        // 分配值：老角色卡没有 allocatedAttributes，回落到 attributes——此时
        // 两者视为相同，跟 attrAfterAge 保持 null 是一致的（wizard-bugfix-
        // round4.md 方案 A）。
        const savedAllocated = saved.allocatedAttributes ?? saved.attributes
        // 优先用后端存的 occupationId（exec/22）：职业名不唯一，按名字 find
        // 只会拿回第一个匹配，同名不同项的职业（律师/私家侦探/工匠…）会被
        // 认成同一个——重新打开向导时选中项就悄悄换成了另一个变体。
        const matchedId =
          saved.occupationId ??
          (saved.occupation
            ? (ruleset.occupations.find((o) => o.name === saved.occupation)?.id ?? null)
            : null)

        const view = await previewCharacter({
          attributes: savedAttrs,
          allocatedAttributes: saved.allocatedAttributes ?? null,
          occupationId: matchedId,
          skills: saved.skills ?? {},
          age: saved.age ?? null,
          // 这条调用漏传这两个字段会导致 roll_pool/roll 角色卡在水合阶段
          // 就退化成空 skillView（跟 wizard-bugfix-round1 修的核心 bug
          // 同一个根因，只是这条独立的 previewCharacter 调用当时没被
          // 一起改到）——编辑一张已保存的掷点池角色卡时，技能加点会在
          // 水合时静默丢失。
          generationMethod: saved.generationMethod,
          attributePoolTotal: saved.attributePoolTotal ?? null,
        })
        if (cancelled) return

        const alloc: Record<string, number> = {}
        for (const v of view.skillView) {
          if (v.allocated > 0) alloc[v.id] = v.allocated
        }

        const patch: WizardHydratePatch = {
          attr: { ...savedAllocated },
          attrInputs: Object.fromEntries(
            ruleset.attributes.filter((a) => a.pointBuy).map((a) => [a.key, String(savedAllocated[a.key] ?? '')])
          ),
          attrAfterAge: saved.allocatedAttributes ? { ...savedAttrs } : null,
          info: {
            name: saved.name ?? '',
            gender: saved.gender ?? '',
            residence: saved.residence ?? '',
            birthplace: saved.birthplace ?? '',
          },
          age: saved.age ?? DEFAULT_AGE,
          occupationId: matchedId,
          skillAlloc: alloc,
          equipment: (saved.equipment ?? []).join('、'),
          background: saved.background ?? '',
          notes: saved.notes ?? '',
          backgroundDetail: { ...emptyBackgroundDetail(), ...(saved.backgroundDetail ?? {}) },
          generationMethod: (saved.generationMethod as GenerationMethod | undefined) ?? 'pointbuy',
          attributePoolTotal:
            (saved.generationMethod as GenerationMethod | undefined) === 'roll_pool'
              ? (saved.attributePoolTotal ?? null)
              : null,
        }
        dispatch({ type: 'HYDRATE', patch })
      })
      .catch(() => {
        // 读不回来（比如还没建过草稿）就沿用空白向导，不打断建卡。
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ruleset, characterId])
}
