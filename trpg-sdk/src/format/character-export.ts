/**
 * 角色卡导出格式化（迁移自用户个人项目 `coc-char-gen` 的
 * `js/plugins/exporters.js`，见 docs/character-build-migration/design.md）。
 *
 * 纯函数：只格式化后端已经算好的权威数据（`CharacterRead` +
 * `CharacterComputeResult`），不重新计算任何 COC7 规则数值——SDK 的定位是
 * "零运行时依赖"，这里也不引入新依赖，全部用标准库字符串/数组操作。
 *
 * `CharacterComputeResult.skillView` 只有技能 `id`（后端 kebab-case slug，
 * 比如 `fighting-brawl`），没有中文显示名——调用方需要额外传入
 * `skills: SkillSpec[]`（`Ruleset.skills`，来自 `GamesResource` 拿到的
 * `RulesetRead`）供这里查名字，不是"为了凑参数瞎编数据"，是显示名这份信息
 * 确实只存在于 ruleset 里。
 */
import type { Character, CharacterComputeResult, SkillComputeView, SkillSpec } from '../types';

/** 8 项基础属性（不含 LUCK——原版 `ATTR_CN` 里 LUC 单独处理，这里保持一致）
 * 的键名 → 中文显示名，用于骰娘导入格式里 `力量50str50` 这类 token。 */
const ATTR_CN: Record<string, string> = {
  STR: '力量',
  CON: '体质',
  SIZ: '体型',
  DEX: '敏捷',
  APP: '外貌',
  INT: '智力',
  POW: '意志',
  EDU: '教育',
};

/** 文本卡属性行的完整顺序（含幸运），对应显示名。 */
const TEXT_CARD_ATTRS: Array<[key: string, label: string]> = [
  ['STR', '力量'],
  ['CON', '体质'],
  ['SIZ', '体型'],
  ['DEX', '敏捷'],
  ['APP', '外貌'],
  ['INT', '智力'],
  ['POW', '意志'],
  ['EDU', '教育'],
  ['LUCK', '幸运'],
];

/**
 * 骰娘常用技能别名展开表（原样照抄 `exporters.js`，这是骰娘生态的通用约定，
 * 不是本项目特有的东西）——键改成后端真实技能 id（kebab-case），原表按
 * 中文技能名做键。
 */
const SKILL_ALIASES: Record<string, string[]> = {
  'computer-use': ['计算机', '电脑', '计算机使用'],
  'library-use': ['图书馆', '图书馆使用'],
  'credit-rating': ['信用', '信誉', '信用评级'],
  'cthulhu-mythos': ['克苏鲁', '克苏鲁神话', 'cm'],
  'drive-auto': ['汽车', '驾驶', '汽车驾驶'],
  'lock-smith': ['开锁', '撬锁', '锁匠'],
  'natural-world': ['博物学', '自然学'],
  navigation: ['导航', '领航'],
  'heavy-machinery': ['重型操作', '重型机械', '操作重型机械', '重型'],
  charm: ['取悦', '魅惑'],
  'fighting-brawl': ['斗殴'],
  'firearm-handgun': ['手枪'],
  'firearm-rifle': ['步枪'],
};

function buildSkillNameMap(skills: SkillSpec[]): Map<string, string> {
  const map = new Map<string, string>();
  for (const skill of skills) {
    map.set(skill.id, skill.name);
  }
  return map;
}

/**
 * 一项技能要导出成哪些 token。原版按 id 精确匹配别名表，命中不了再按
 * `格斗:xxx`/`射击:xxx` 这类复合名拆出短名。本项目的复合技能名用全角冒号
 * （`格斗：斗殴`/`射击：步枪/霰弹枪`/`科学：xxx`/`驾驶：xxx`），統一按 name
 * 里的全角冒号拆分即可，不需要 JS 版本里针对 fighting/firearm 的分支判断。
 */
function skillExportTokens(id: string, total: number, name: string): string[] {
  const aliases = SKILL_ALIASES[id];
  if (aliases) {
    return aliases.map((alias) => `${alias}${total}`);
  }
  if (name.includes('：')) {
    const short = name.split('：')[1] ?? name;
    return [`${short}${total}`];
  }
  return [`${name || id}${total}`];
}

function numDerived(compute: CharacterComputeResult, key: string): number {
  const value = compute.derivedStats[key];
  return typeof value === 'number' ? value : 0;
}

/**
 * 骰娘完整导入格式（`.st` 全量）。跳过 0 值/未加点的次要技能，但信用评级、
 * 有加点的技能、以及有基础值的非复合技能都导出，供大多数骰娘 `.st` 指令
 * 一次性导入。
 */
export function formatDicebotFull(
  character: Character,
  compute: CharacterComputeResult,
  skills: SkillSpec[]
): string {
  const attrs = character.attributes ?? {};
  const parts: string[] = [];

  for (const [key, cn] of Object.entries(ATTR_CN)) {
    const v = attrs[key] ?? 0;
    parts.push(`${cn}${v}${key.toLowerCase()}${v}`);
  }
  const int = attrs.INT ?? 0;
  parts.push(`智力${int}灵感${int}int${int}`);
  const san = numDerived(compute, 'SAN');
  parts.push(`san${san}san值${san}理智${san}理智值${san}`);
  const luck = attrs.LUCK ?? 0;
  parts.push(`幸运${luck}运气${luck}`);
  const mp = numDerived(compute, 'MP');
  parts.push(`mp${mp}魔法${mp}`);
  const hp = numDerived(compute, 'HP');
  parts.push(`hp${hp}体力${hp}`);

  const nameById = buildSkillNameMap(skills);
  for (const sk of compute.skillView) {
    const name = nameById.get(sk.id) ?? sk.id;
    if (sk.id === 'credit-rating' || sk.current > sk.base) {
      parts.push(...skillExportTokens(sk.id, sk.current, name));
    } else if (sk.base > 0 && !name.includes('：')) {
      parts.push(...skillExportTokens(sk.id, sk.current, name));
    }
  }

  const body = parts.join('');
  return character.name ? `.st ${character.name}-${body}` : `.st ${body}`;
}

/** 骰娘精简导入格式：属性用全名，技能只导出有加点的（+ 信用评级）。 */
export function formatDicebotShort(
  character: Character,
  compute: CharacterComputeResult,
  skills: SkillSpec[]
): string {
  const attrs = character.attributes ?? {};
  const int = attrs.INT ?? 0;
  const parts = [
    `力量${attrs.STR ?? 0}`,
    `敏捷${attrs.DEX ?? 0}`,
    `意志${attrs.POW ?? 0}`,
    `体质${attrs.CON ?? 0}`,
    `外貌${attrs.APP ?? 0}`,
    `教育${attrs.EDU ?? 0}`,
    `体型${attrs.SIZ ?? 0}`,
    `智力${int}`,
    `灵感${int}`,
    `理智${numDerived(compute, 'SAN')}`,
    `幸运${attrs.LUCK ?? 0}`,
    `魔法${numDerived(compute, 'MP')}`,
    `hp${numDerived(compute, 'HP')}`,
    `体力${numDerived(compute, 'HP')}`,
  ];

  const nameById = buildSkillNameMap(skills);
  for (const sk of compute.skillView) {
    if (sk.current <= sk.base && sk.id !== 'credit-rating') continue;
    const name = nameById.get(sk.id) ?? sk.id;
    const short = name.replace(/[：:]/g, '');
    parts.push(`${short}${sk.current}`);
  }

  const head = character.name ? `.st ${character.name}-` : '.st ';
  return head + parts.join('');
}

/** 人类可读文本角色卡。 */
export function formatTextCard(
  character: Character,
  compute: CharacterComputeResult,
  skills: SkillSpec[]
): string {
  const attrs = character.attributes ?? {};
  const lines: string[] = [];
  lines.push(`【${character.name || '未命名调查员'}】`);
  lines.push(`年龄:${character.age ?? '-'} | 职业:${character.occupation || '无'}`);
  lines.push(TEXT_CARD_ATTRS.map(([key, label]) => `${label}${attrs[key] ?? 0}`).join(' '));
  lines.push(
    `生命${numDerived(compute, 'HP')} 魔法${numDerived(compute, 'MP')} ` +
      `理智${numDerived(compute, 'SAN')} 伤害加值${compute.derivedStats.DB ?? ''} ` +
      `体格${compute.derivedStats.Build ?? ''} 移动${numDerived(compute, 'MOV')}`
  );
  lines.push(
    `职业点 ${compute.occupationSkillPoints.spent}/${compute.occupationSkillPoints.budget} | ` +
      `兴趣点 ${compute.interestSkillPoints.spent}/${compute.interestSkillPoints.budget}`
  );
  lines.push('--- 技能 ---');

  const nameById = buildSkillNameMap(skills);
  const usedSkills = compute.skillView
    .filter((sk: SkillComputeView) => sk.current > sk.base || sk.id === 'credit-rating')
    .sort((a, b) => b.current - a.current);
  for (const sk of usedSkills) {
    const name = nameById.get(sk.id) ?? sk.id;
    const half = Math.floor(sk.current / 2);
    const fifth = Math.floor(sk.current / 5);
    lines.push(`${name} ${sk.current}（困难${half}/极难${fifth}） 已加点+${sk.allocated}`);
  }

  if (compute.validation.length > 0) {
    lines.push('--- 校验提示 ---');
    for (const issue of compute.validation) {
      lines.push(`! ${issue.message}`);
    }
  }
  return lines.join('\n');
}

/** 数据备份：完整角色数据 + 计算结果，便于再导入。 */
export function formatCharacterJson(character: Character, compute: CharacterComputeResult): string {
  return JSON.stringify({ character, compute }, null, 2);
}
