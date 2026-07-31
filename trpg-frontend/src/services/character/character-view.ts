import type { Character, Ruleset } from 'trpg-sdk';
import type { BackgroundDetail } from '@/data/character-model';
import type { CompletedCharacter } from '@/stores/character-store';

/**
 * 后端角色卡（权威） → 前端渲染用的形状。
 *
 * ## 为什么需要这层转换
 *
 * 后端存的是"数据"，前端渲染的是"视图"，两边形状本来就不同：
 * - `occupation` 是**职业名**（后端 `Character.occupation` 存名字不是 id，
 *   见模型注释），前端各处要拿 `occupationId` 去 ruleset 里查职业技能；
 * - `equipment` 后端是字符串数组，前端一直按一段文本渲染；
 * - `derivedStats` 是个宽松的 `dict[str, int|str]`，前端要的是固定五项。
 *
 * ## 🔴 `skillAlloc` 恒为空，这是有意的
 *
 * 后端只存技能**终值**（base+分配），不存"玩家分配了多少点"。局内展示只需要
 * 终值，所以这里给空对象；**建卡向导编辑已有角色卡**那条路径仍然需要分配值，
 * 它继续从 `character-store` 的本地草稿读（见该文件说明）。别把这里的空
 * `skillAlloc` 喂给向导。
 */
export function toCompletedCharacter(
  saved: Character,
  ruleset: Ruleset
): CompletedCharacter | null {
  if (!saved.name) return null;

  const occupationId = ruleset.occupations.find((o) => o.name === saved.occupation)?.id ?? null;
  const derived = saved.derivedStats ?? {};
  const num = (v: unknown) => (typeof v === 'number' ? v : 0);

  return {
    info: {
      name: saved.name,
      playerName: '',
      age: saved.age != null ? String(saved.age) : '',
      gender: saved.gender ?? '',
      residence: saved.residence ?? '',
      birthplace: saved.birthplace ?? '',
      occupationId,
    },
    attr: { ...saved.attributes },
    skillAlloc: {},
    skillFinalValues: { ...saved.skills },
    equipment: (saved.equipment ?? []).join('、'),
    background: saved.background ?? '',
    notes: saved.notes ?? '',
    derived: {
      hp: num(derived.HP),
      san: num(derived.SAN),
      mp: num(derived.MP),
      db: derived.DB == null ? '0' : String(derived.DB),
      move: num(derived.MOV),
    },
    // 后端把这 8 个引导字段当**不透明字典**存取（不做逐键校验，键的含义是
    // 前端表单的事）。渲染侧统一走 BACKGROUND_DETAIL_FIELDS 逐键取值，缺键
    // 就是 undefined、自然被过滤掉，所以这个断言不会造成运行时问题。
    backgroundDetail: (saved.backgroundDetail as BackgroundDetail | null) ?? undefined,
  };
}
