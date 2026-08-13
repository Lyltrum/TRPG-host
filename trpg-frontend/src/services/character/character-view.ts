import type { Character, CharacterTemplate, Ruleset } from 'trpg-sdk';
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

  // 优先用后端存的 id（exec/22）。名字不唯一——按名字 find 只会拿回第一个
  // 匹配，同名不同项的职业（律师/私家侦探/工匠…）会被认成同一个。老卡没有
  // id 时才回退按名字查，行为与改动前一致。
  const occupationId =
    saved.occupationId ?? ruleset.occupations.find((o) => o.name === saved.occupation)?.id ?? null;
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
    // 🔴 血条的分母走这个字段，**不要**拿 derived.HP 当上限：守秘人扣过血
    // 之后那一格是当前值（exec/26 #67）。
    hpMax: saved.hpMax ?? null,
    // 后端把这 8 个引导字段当**不透明字典**存取（不做逐键校验，键的含义是
    // 前端表单的事）。渲染侧统一走 BACKGROUND_DETAIL_FIELDS 逐键取值，缺键
    // 就是 undefined、自然被过滤掉，所以这个断言不会造成运行时问题。
    backgroundDetail: (saved.backgroundDetail as BackgroundDetail | null) ?? undefined,
  };
}

/**
 * 卡库列表项的副标题：「私家侦探 · 32 岁 · 存于 8/13」。
 *
 * 卡库页与建卡向导的选择浮层**共用这一份**——两处显示的是同一份数据的同一种
 * 摘要，各写一份就是「两处必须一致」，改一处漏一处不会有任何东西变红。
 *
 * 🔴 **重名是合法用法，不是要消除的异常**（用户 2026-08-13）：玩家可以把
 * 五张预设卡全叫「凌铭辉」。所以这一行必须保证**完全重名时也分得出来**——
 * 职业、年龄、存入日期依次是三道分辨手段。
 *
 * 🔴 角色名**按条件**去掉，不是无条件：卡库名默认就等于角色名，那时标题
 * 和副标题字对字重复、白占一行；但一旦两者不同（将来支持重命名），角色名
 * 就是有效信息，要显示出来。
 */
export function templateSubtitle(template: CharacterTemplate): string {
  const data = (template.data ?? {}) as Record<string, unknown>;
  const roleName = typeof data.name === 'string' ? data.name : null;
  const parts = [
    roleName && roleName !== template.name ? roleName : null,
    typeof data.occupation === 'string' ? data.occupation : null,
    typeof data.age === 'number' ? `${data.age} 岁` : null,
    templateSavedOn(template.createdAt),
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : '调查员';
}

/** 「存于 8/13」。日期是职业年龄都撞车时（同一个调查员存了两次）最后的分辨手段。 */
function templateSavedOn(createdAt: string): string | null {
  const parsed = Date.parse(createdAt);
  if (Number.isNaN(parsed)) return null;
  const d = new Date(parsed);
  return `存于 ${d.getMonth() + 1}/${d.getDate()}`;
}
