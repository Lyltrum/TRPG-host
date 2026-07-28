/**
 * 角色卡导出格式化的单元测试（character-build-migration Phase 2）。
 *
 * 只断言输出包含关键 token，不逐字符比对整份字符串——格式化模板本身可能
 * 随需求调整措辞，逐字比对会让测试变得脆弱又不真正验证"信息有没有传递到"。
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { Character, CharacterComputeResult, SkillSpec } from '../types';
import {
  formatCharacterJson,
  formatDicebotFull,
  formatDicebotShort,
  formatTextCard,
} from './character-export';

const character: Character = {
  id: 'char-1',
  status: 'completed',
  generationMethod: 'pointbuy',
  name: '压测员',
  age: 27,
  gender: '不明',
  residence: '阿卡姆',
  birthplace: '波士顿',
  attributes: {
    STR: 50,
    CON: 60,
    SIZ: 65,
    DEX: 55,
    APP: 45,
    INT: 70,
    POW: 60,
    EDU: 75,
    LUCK: 50,
  },
  derivedStats: {
    HP: 12,
    MP: 12,
    SAN: 60,
    DB: '+1D4',
    Build: 1,
    MOV: 8,
  },
  skills: {},
  equipment: [],
  occupation: '记者',
  background: '',
  notes: '',
  backgroundDetail: null,
};

const compute: CharacterComputeResult = {
  derivedStats: character.derivedStats!,
  occupationSkillPoints: { budget: 280, spent: 200, remaining: 80 },
  interestSkillPoints: { budget: 100, spent: 20, remaining: 80 },
  skillView: [
    { id: 'credit-rating', base: 0, allocated: 30, current: 30, cap: 99 },
    { id: 'computer-use', base: 5, allocated: 40, current: 45, cap: 99 },
    { id: 'fighting-brawl', base: 25, allocated: 10, current: 35, cap: 99 },
    { id: 'accounting', base: 5, allocated: 0, current: 5, cap: 99 },
  ],
  validation: [],
};

const skills: SkillSpec[] = [
  { id: 'credit-rating', name: '信用评级', base: 0, category: 'social' },
  { id: 'computer-use', name: '计算机使用', base: 5, category: 'technical' },
  { id: 'fighting-brawl', name: '格斗：斗殴', base: 25, category: 'combat' },
  { id: 'accounting', name: '会计', base: 5, category: 'knowledge' },
];

test('formatDicebotFull：包含姓名前缀、核心属性、衍生值与技能别名', () => {
  const text = formatDicebotFull(character, compute, skills);
  assert.match(text, /^\.st 压测员-/);
  assert.match(text, /力量50/);
  assert.match(text, /san60/);
  assert.match(text, /幸运50/);
  assert.match(text, /hp12/);
  // 计算机使用 → 别名表命中，导出为"计算机"/"电脑"
  assert.match(text, /计算机45/);
  assert.match(text, /电脑45/);
  // 信用评级即使 base=0 也强制导出
  assert.match(text, /信用30/);
  // 复合名技能（有加点）按全角冒号拆出短名
  assert.match(text, /斗殴35/);
  // 未加点的基础技能（会计）仍导出
  assert.match(text, /会计5/);
});

test('formatDicebotShort：只导出有加点的技能与信用评级', () => {
  const text = formatDicebotShort(character, compute, skills);
  assert.match(text, /^\.st 压测员-/);
  assert.match(text, /力量50/);
  assert.match(text, /理智60/);
  // 短格式不查别名表，技能名只做去冒号处理
  assert.match(text, /信用评级30/);
  assert.match(text, /计算机使用45/);
  // 未加点、非信用评级的技能（会计）不导出
  assert.doesNotMatch(text, /会计5/);
});

test('formatTextCard：包含姓名、属性行、衍生值与已加点技能', () => {
  const text = formatTextCard(character, compute, skills);
  assert.match(text, /【压测员】/);
  assert.match(text, /职业:记者/);
  assert.match(text, /力量50/);
  assert.match(text, /幸运50/);
  assert.match(text, /生命12 魔法12 理智60 伤害加值\+1D4 体格1 移动8/);
  assert.match(text, /职业点 200\/280/);
  assert.match(text, /信用评级 30/);
  assert.match(text, /格斗：斗殴 35/);
});

test('formatCharacterJson：可反解析，且携带角色与计算结果', () => {
  const text = formatCharacterJson(character, compute);
  const parsed = JSON.parse(text) as { character: Character; compute: CharacterComputeResult };
  assert.equal(parsed.character.name, '压测员');
  assert.equal(parsed.compute.derivedStats.SAN, 60);
});
