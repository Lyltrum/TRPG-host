import type {
  AgeAdjustmentResult,
  CharacterTemplate,
  RollAttributePoolResult,
  RollAttributesResult,
  RollLuckResult,
} from 'trpg-sdk';
import type { BackgroundDetail, GenerationMethod } from '@/data/character-model';
import { useRoomStore } from '@/stores/room-store';
import { getAuthToken, sdk } from '../api-client';
import { resolveSystemId } from './ruleset-api';

// 真实建卡流程对接：POST 建草稿 → PATCH 填数据 → POST complete 完成。

export interface BuiltCharacter {
  name: string;
  // 基本信息也要存后端：此前它们只活在前端本地状态里，清掉缓存就丢，
  // 而「角色卡以后端为唯一事实来源」要求这些也归后端（issue #96）。
  age: number | null;
  gender: string | null;
  residence: string;
  birthplace: string;
  attr: Record<string, number>; // 后端属性键，如 { STR: 50, CON: 60, ... }
  // 分配值（wizard-bugfix-round4.md 方案 A）：玩家分配的原始属性，年龄
  // 修正/规则校验的计算基准；`attr` 存的是有效值（年龄修正后）。不传即
  // 退回"两者相同"的旧行为（后端语义，见 CharacterUpdateBody 字段说明）。
  allocatedAttributes?: Record<string, number> | null;
  derived: { hp: number; san: number; mp: number };
  skillValues: Record<string, number>; // skillId -> 最终值（base+分配）
  equipment: string;
  // 🔴 职业用 id 定位（exec/22）：职业名不唯一——规则表里有 6 组同名不同项的
  // 职业，信用区间乃至技能点公式都不同。向导在预览步骤本来就拿着 id，保存时
  // 也要带上；只传名字的话"玩家选的是哪一个"在落库那一刻就丢了。
  occupationId: number | null;
  occupationName: string | null;
  background: string;
  notes: string;
  // 结构化背景故事（character-build-migration），可选——不是每次保存都填过。
  backgroundDetail?: BackgroundDetail;
  // 前端当前显示的生成方式（character-build-migration 已知缺口修复）：只有
  // 'pointbuy' 会被后端信任并写入（玩家点"点数购买"按钮改回手动分配时，
  // 让后端知道不用再按掷点池的"总和精确匹配"校验），见
  // CharacterUpdateBody.generation_method 的字段说明。
  generationMethod: GenerationMethod;
}

// 建卡接口跟房间模块一样，靠 X-Reconnect-Token 确认"你是这个房间里的哪个玩家"。
function requireReconnectToken(): string {
  const token = useRoomStore.getState().reconnectToken;
  if (!token) throw new Error('缺少房间重连凭证，请重新加入房间');
  return token;
}

export async function createCharacterDraft(roomId: string): Promise<string> {
  const res = await sdk.characters.createDraft(roomId, requireReconnectToken());
  return res.characterId;
}

/**
 * 一键生成一张合法角色卡（给零基础玩家的第二条建卡路径）。
 *
 * 属性/职业/技能全部由**服务端**随机生成，返回时这张卡已经是完成态——前端
 * 不参与任何数值决定，也不需要再走 complete。玩家只提供名字。
 */
export async function quickBuildCharacter(roomId: string, name: string): Promise<string> {
  const res = await sdk.characters.quickBuild(roomId, requireReconnectToken(), { name });
  return res.characterId;
}

export async function saveCharacter(
  roomId: string,
  characterId: string,
  built: BuiltCharacter
): Promise<void> {
  await sdk.characters.save(
    roomId,
    characterId,
    {
      name: built.name,
      age: built.age,
      gender: built.gender,
      residence: built.residence,
      birthplace: built.birthplace,
      attributes: built.attr,
      allocatedAttributes: built.allocatedAttributes ?? null,
      derivedStats: { HP: built.derived.hp, SAN: built.derived.san, MP: built.derived.mp },
      skills: built.skillValues,
      equipment: built.equipment
        ? built.equipment
            .split(/[,，\n]/)
            .map((s) => s.trim())
            .filter(Boolean)
            .map((name) => ({ name }))
        : [],
      occupationId: built.occupationId,
      occupation: built.occupationName,
      background: built.background,
      notes: built.notes,
      backgroundDetail: (built.backgroundDetail as Record<string, string> | undefined) ?? null,
      generationMethod: built.generationMethod
    },
    requireReconnectToken()
  );
}

/**
 * 从后端读回一张已保存的角色卡（issue #96）。
 *
 * 后端是角色卡的唯一事实来源。此前前端把整张卡存在 localStorage 里当权威源，
 * 那份副本的结构会随后端 schema 演进而过期——PR #88 给属性加了幸运之后，本地
 * 存的 8 键旧卡再打开就被后端的 9 键校验拒了，玩家的卡直接编辑不了。
 */
export async function fetchCharacter(roomId: string, characterId: string) {
  return sdk.characters.get(roomId, characterId, requireReconnectToken());
}

export async function completeCharacter(roomId: string, characterId: string): Promise<void> {
  await sdk.characters.complete(roomId, characterId, requireReconnectToken());
}

/** 重摇一次角色背景（exec/25 P1 #5）。只换过去，属性/技能/职业不动。 */
export async function regenerateBackground(roomId: string, characterId: string) {
  return sdk.characters.regenerateBackground(roomId, characterId, requireReconnectToken());
}

/**
 * 服务端权威掷骰生成 8+1 项属性（character-build-migration）。跟其余四个
 * 函数保持同样的薄封装风格，页面组件不直接碰 sdk/reconnectToken。
 */
export async function rollAttributes(roomId: string, characterId: string): Promise<RollAttributesResult> {
  return sdk.characters.rollAttributes(roomId, characterId, requireReconnectToken());
}

/** 掷点池生成法：服务端掷出总点数池，分配到八维由前端向导完成。 */
export async function rollAttributePool(
  roomId: string,
  characterId: string
): Promise<RollAttributePoolResult> {
  return sdk.characters.rollAttributePool(roomId, characterId, requireReconnectToken());
}

/** 幸运单掷（character-build-migration redesign-v2 §4-A）：独立于生成方式，
 * 点数购买/掷骰/掷点池三种生成法都能调这个端点掷幸运。 */
export async function rollLuck(roomId: string, characterId: string): Promise<RollLuckResult> {
  return sdk.characters.rollLuck(roomId, characterId, requireReconnectToken());
}

/** 套用 COC7 建卡期年龄修正（EDU 改进检定/身体减值/外貌减值/青年幸运双掷）。 */
export async function applyAgeAdjustment(
  roomId: string,
  characterId: string,
  age: number
): Promise<AgeAdjustmentResult> {
  return sdk.characters.applyAgeAdjustment(roomId, characterId, age, requireReconnectToken());
}

// ── 我的常用角色卡（卡库）─────────────────────────────────────────
//
// 线下的老玩家会带着自己的调查员来。这一晚开第二局、或者换个模组重开时，
// 他不想再走一遍八步向导。
//
// 🔴 **模板是复制一份新的**，不是同一个调查员带着成长回来——后者要战役支持，
// 是另一件事。所以复用出来的仍是 draft，complete 时那套校验一条都不少。

function requireAuthToken(): string {
  const token = getAuthToken();
  if (!token) throw new Error('常用卡属于账号，请先登录');
  return token;
}

/** 我的卡库，最近更新的在前。 */
export async function listMyTemplates(): Promise<CharacterTemplate[]> {
  return sdk.characterTemplates.list(requireAuthToken());
}

/**
 * 这个规则系统下**能用**的常用卡（建卡向导的挑卡浮层）。
 *
 * 🔴 过滤条件送去后端，不在这里筛：判据是后端那条「这张常用卡不适用于本房间的
 * 规则系统」，前端再实现一遍就是同一条规则落两处。此前浮层列的是全部卡，玩家
 * 点到用不了的那张只会拿到一个报错。
 */
export async function listUsableTemplates(): Promise<CharacterTemplate[]> {
  const token = requireAuthToken();
  return sdk.characterTemplates.list(token, await resolveSystemId());
}

/**
 * 把一张已建好的卡存进卡库。
 *
 * 只传 `characterId`：**存哪些字段由后端决定**（规则权威在后端），前端不拼
 * `data`——那样 Character 加一列就要两边同时改，漏一边不会有任何东西变红。
 */
export async function saveAsTemplate(
  characterId: string,
  name: string
): Promise<CharacterTemplate> {
  return sdk.characterTemplates.save({ name, characterId }, requireAuthToken());
}

/** 卡库详情：单张常用卡。 */
export async function fetchMyTemplate(templateId: string): Promise<CharacterTemplate> {
  return sdk.characterTemplates.get(templateId, requireAuthToken());
}

/**
 * 改卡库里那张卡的文字部分。
 *
 * 🔴 `data` 是**部分更新**，而且只收文字字段（姓名/性别/居住地/出生地/背景）。
 * 属性、年龄、职业、技能后端会**显式拒绝**——那些改一处就要重跑整套 COC7 校验
 * 与年龄修正，那条链路长在建卡向导上，不在卡库里再造一套。
 */
export async function updateMyTemplate(
  templateId: string,
  patch: { name?: string; data?: Record<string, unknown> }
): Promise<CharacterTemplate> {
  return sdk.characterTemplates.update(templateId, patch, requireAuthToken());
}

/**
 * 用这张角色卡的当前状态整份覆盖卡库里那张（「改完了，更新我卡库里那张」）。
 *
 * 跟 `updateMyTemplate` 的分工不在改多少，在**数据谁给的**：那条发前端填的
 * 文字字段，这条只发 characterId、建卡态由后端从角色卡读。
 */
export async function overwriteMyTemplate(
  templateId: string,
  characterId: string
): Promise<CharacterTemplate> {
  return sdk.characterTemplates.overwrite(templateId, { characterId }, requireAuthToken());
}

export async function deleteMyTemplate(templateId: string): Promise<void> {
  await sdk.characterTemplates.remove(templateId, requireAuthToken());
}

/** 用常用卡开一份新草稿，返回 characterId。 */
export async function createDraftFromTemplate(
  roomId: string,
  templateId: string
): Promise<{ characterId: string; status: string }> {
  // 🔴 status 要带回去：常用卡合法时后端**直接建成 complete**，玩家不该再被
  // 赶进向导（2026-08-13 真人反馈）。只有校验没过才是 draft，那时才进向导修。
  const res = await sdk.characters.createDraft(roomId, requireReconnectToken(), templateId);
  return { characterId: res.characterId, status: res.status };
}
