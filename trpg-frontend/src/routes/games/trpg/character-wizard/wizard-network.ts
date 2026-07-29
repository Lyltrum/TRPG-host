import type { SkillComputeView } from 'trpg-sdk'
import { useRoomStore } from '@/stores/room-store'
import { createCharacterDraft, saveCharacter } from '@/services/character/character-api'
import type { WizardState } from './wizard-state'

/**
 * 建卡向导里几处步骤（属性掷骰/年龄调整/最终提交）共用的网络编排小工具。
 * 不是 hook——没有 React 状态，纯粹是"避免同一段逻辑在多个 step 组件里
 * 各写一份"（`ensureCharacterId`/`syncCurrentStateToBackend` 原来在
 * CharacterPage.tsx 里各只有一份，拆分成 steps 后需要一个共享落点）。
 */

/** 已有草稿就复用同一个 characterId，不要每次调用都新建一条。 */
export async function ensureCharacterId(roomId: string): Promise<string> {
  const existing = useRoomStore.getState().characterId
  if (existing) return existing
  const id = await createCharacterDraft(roomId)
  useRoomStore.getState().setCharacterId(id)
  return id
}

/**
 * skillAlloc（本地记的"加了多少点"）换算成"最终值"（base + 加点）——
 * 后端 PATCH/preview 要的是最终值，不是加点数。
 */
export function buildSkillsPayload(
  skillAlloc: Record<string, number>,
  skillComputeMap: Map<string, SkillComputeView>
): Record<string, number> {
  const payload: Record<string, number> = {}
  for (const [id, pts] of Object.entries(skillAlloc)) {
    if (!pts) continue
    const base = skillComputeMap.get(id)?.base ?? 0
    payload[id] = base + pts
  }
  return payload
}

/**
 * 把向导当前的本地状态同步一份到后端。年龄调整端点作用于**后端已保存**的
 * `character.attributes`，而属性分配这一步全程只改本地 state、不落库——
 * 不先同步一次的话，年龄调整会套在一份陈旧（甚至是空）的属性上。
 */
export async function syncCurrentStateToBackend(
  roomId: string,
  characterId: string,
  state: WizardState,
  derived: { hp: number; san: number; mp: number },
  skillComputeMap: Map<string, SkillComputeView>,
  occupationName: string | null
): Promise<void> {
  await saveCharacter(roomId, characterId, {
    name: state.info.name,
    age: state.age,
    gender: state.info.gender || null,
    residence: state.info.residence,
    birthplace: state.info.birthplace,
    attr: state.attr,
    derived,
    skillValues: buildSkillsPayload(state.skillAlloc, skillComputeMap),
    equipment: state.equipment,
    occupationName,
    background: state.background,
    notes: state.notes,
    backgroundDetail: state.backgroundDetail,
    generationMethod: state.generationMethod,
  })
}
