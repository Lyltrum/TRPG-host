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
export function useWizardHydration(ruleset: Ruleset | null, dispatch: (action: WizardAction) => void) {
  useEffect(() => {
    const roomId = useRoomStore.getState().roomId
    const characterId = useRoomStore.getState().characterId
    if (!roomId || !characterId || !ruleset) return

    let cancelled = false
    fetchCharacter(roomId, characterId)
      .then(async (saved) => {
        if (cancelled || !saved.attributes || Object.keys(saved.attributes).length === 0) return
        const savedAttrs = saved.attributes
        const matched = saved.occupation ? (ruleset.occupations.find((o) => o.name === saved.occupation) ?? null) : null

        const view = await previewCharacter({
          attributes: savedAttrs,
          occupationId: matched?.id ?? null,
          skills: saved.skills ?? {},
          age: saved.age ?? null,
        })
        if (cancelled) return

        const alloc: Record<string, number> = {}
        for (const v of view.skillView) {
          if (v.allocated > 0) alloc[v.id] = v.allocated
        }

        const patch: WizardHydratePatch = {
          attr: { ...savedAttrs },
          attrInputs: Object.fromEntries(
            ruleset.attributes.filter((a) => a.pointBuy).map((a) => [a.key, String(savedAttrs[a.key] ?? '')])
          ),
          info: {
            name: saved.name ?? '',
            gender: saved.gender ?? '',
            residence: saved.residence ?? '',
            birthplace: saved.birthplace ?? '',
          },
          age: saved.age ?? DEFAULT_AGE,
          occupationId: matched?.id ?? null,
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
  }, [ruleset])
}
