import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Ruleset, SkillComputeView } from 'trpg-sdk'
import { useCharacterStore } from '@/stores/character-store'
import { useRoomStore } from '@/stores/room-store'
import { completeCharacter, createCharacterDraft, saveCharacter } from '@/services/character/character-api'
import { previewCharacter, translateCharacterValidationError } from '@/services/character/ruleset-api'
import { effectiveAttr, normalizeDerivedStats } from './wizard-selectors'
import { buildSkillsPayload } from './wizard-network'
import type { WizardState } from './wizard-state'

/**
 * 完成页的提交流程（重制设计 v2 §6-步骤7 第 5 条"这一整段保留不动"）：
 * 最终 previewCharacter 复算 → 复用或创建 characterId → saveCharacter →
 * completeCharacter → 写 character-store → 跳转。
 */
export function useWizardSubmit(
  ruleset: Ruleset | null,
  state: WizardState,
  skillComputeMap: Map<string, SkillComputeView>,
  occupationName: string | null
) {
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')

  const handleSubmit = async () => {
    const roomId = useRoomStore.getState().roomId
    if (!roomId) {
      setSubmitError('房间信息丢失，请重新创建/加入房间')
      return
    }
    if (!ruleset) return

    // 转换失败（skillComputeMap 还没有某个技能的权威 base）绝不能静默丢掉
    // 玩家的加点、也不能把加点当绝对值发出去（exec/12 #19）——中止提交，明确
    // 提示，让玩家重试。
    let skillsPayload: Record<string, number>
    try {
      skillsPayload = buildSkillsPayload(state.skillAlloc, skillComputeMap)
    } catch {
      setSubmitError('规则数据还没加载完，请稍候重试')
      return
    }

    setSubmitting(true)
    setSubmitError('')
    try {
      // 最终提交前再拉一次权威计算：用当前 base（来自 skillComputeMap）把
      // 已分配点数换算成"最终值"，连同属性/职业一起发给后端拿回完整的
      // skillView 和衍生值，两边都以这次结果为准落库。
      const finalPreview = await previewCharacter({
        attributes: effectiveAttr(state),
        allocatedAttributes: state.attr,
        occupationId: state.occupationId,
        skills: skillsPayload,
        age: state.age,
        // 🔴 漏传这两个字段会让后端按点数购买法的 480 预算去校验掷点池角色
        // （总和常常 >480）→ 校验失败 → `_compute` 整体短路返回全零结果
        // （derivedStats 空、skillView 空、两个预算 0）→ 全零被写进
        // character-store → 游戏内 HP/SAN/技能面板全是 0。数据库里反而是对的，
        // 因为 complete 会服务端重算，掩盖了这个前端 bug。
        // 这是同一个根因第三次出现（round1 修 useWizardPreview、round4 补
        // useWizardHydration，都漏了这条提交路径）——三个调用点现在齐了。
        generationMethod: state.generationMethod,
        attributePoolTotal: state.attributePoolTotal,
      })

      // 落库路径不接受"降级结果"：validation 非空时后端返回的是全零短路结果，
      // 写进 store 就是一张废卡。渲染路径（各 step 组件）可以容忍 preview 缺失
      // 显示 0——那只是加载中；提交路径不行，必须失败要响（round3 判据：
      // 静默兜底是同一个 bug 反复换皮的元凶）。
      if (finalPreview.validation.length > 0) {
        setSubmitError(`角色卡未通过校验：${finalPreview.validation.map((v) => v.message).join('；')}`)
        return
      }
      const finalDerived = normalizeDerivedStats(finalPreview.derivedStats)
      const skillFinalValues = Object.fromEntries(finalPreview.skillView.map((v) => [v.id, v.current]))

      // 已经有草稿就复用，不要每次提交都新建一条。
      const characterId = useRoomStore.getState().characterId ?? (await createCharacterDraft(roomId))
      await saveCharacter(roomId, characterId, {
        name: state.info.name,
        age: state.age,
        gender: state.info.gender || null,
        residence: state.info.residence,
        birthplace: state.info.birthplace,
        attr: effectiveAttr(state),
        allocatedAttributes: state.attr,
        derived: { hp: finalDerived.hp, san: finalDerived.san, mp: finalDerived.mp },
        skillValues: skillsPayload,
        equipment: state.equipment,
        occupationName,
        background: state.background,
        notes: state.notes,
        backgroundDetail: state.backgroundDetail,
        generationMethod: state.generationMethod,
      })
      await completeCharacter(roomId, characterId)
      useRoomStore.getState().setCharacterId(characterId)
      useCharacterStore.getState().setCharacter(
        {
          info: {
            name: state.info.name,
            playerName: state.info.playerName || state.info.name,
            age: String(state.age),
            gender: state.info.gender,
            residence: state.info.residence,
            birthplace: state.info.birthplace,
            occupationId: state.occupationId,
          },
          attr: { ...effectiveAttr(state) },
          skillAlloc: { ...state.skillAlloc },
          skillFinalValues,
          equipment: state.equipment,
          background: state.background,
          notes: state.notes,
          derived: finalDerived,
          backgroundDetail: state.backgroundDetail,
        },
        roomId
      )
      navigate('/room/ready')
    } catch (err) {
      setSubmitError(translateCharacterValidationError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return { handleSubmit, submitting, submitError }
}
