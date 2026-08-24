import type {
  CreateRoomResult,
  ModuleSummary,
  MyRoomSummary,
  RoomPlayerSummary,
  RoomPreview,
  RoomSummary,
} from 'trpg-sdk';
import { DEFAULT_MAX_PLAYERS, useRoomStore } from '@/stores/room-store';
import { getAuthToken, sdk } from './api-client';

export type { CreateRoomResult, ModuleSummary, MyRoomSummary, RoomPreview, RoomSummary };

// 房主/已加入玩家专属的操作（选模组/开始游戏/结束游戏/我的房间列表）需要
// 后端的房间重连凭证（X-Reconnect-Token，issue #39），加入/创建房间时签发、
// 存进 room-store——直接从 store 读，页面组件不需要在每次调用时手动传。
function requireReconnectToken(): string {
  const token = useRoomStore.getState().reconnectToken;
  if (!token) throw new Error('缺少房间重连凭证，请重新加入房间');
  return token;
}

// 创建/加入房间和「我的游戏」用的是**账号**凭证，不是上面那个房间凭证
// （issue #106）。两者分工：账号解决跨设备/跨时间找回，reconnectToken 解决
// 同一局进行中的快速重连。
function requireAuthToken(): string {
  const token = getAuthToken();
  if (!token) throw new Error('请先登录');
  return token;
}

// 创建房间（房主创建即加入，见 §5.2.5）
export async function createGameRoom(
  nickname?: string,
  roomName?: string,
  maxPlayers?: number
): Promise<CreateRoomResult> {
  return sdk.rooms.create(
    { nickname, roomName: roomName ?? '', maxPlayers: maxPlayers ?? DEFAULT_MAX_PLAYERS },
    requireAuthToken()
  );
}

// 拉取可用模组列表：内置的 + 我自己导入的。
// 🔴 必须带账号凭证——导入的模组归导入者所有，服务端认不出你是谁就只给内置那几个。
export async function listModules(): Promise<ModuleSummary[]> {
  return sdk.rooms.listModules(getAuthToken() ?? undefined);
}

// 房主确定模组
export async function selectModule(roomId: string, moduleId: string): Promise<void> {
  await sdk.rooms.selectModule(
    roomId,
    { moduleId, attributeGenMethod: 'point_buy' },
    requireReconnectToken()
  );
}

// 用房间码加入房间。issue #106 起后端按**账号**幂等：已经是这个房间的成员时
// 原样返回既有身份，所以这个函数同时承担「加入」和「掉线/换设备后重连」两个
// 用途——之前那句「已是本房间玩家则幂等返回已有身份」的注释是假的，后端当时
// 根本不检查，重复调用会给同一个人建重复玩家行。
export async function joinRoomByCode(
  roomCode: string,
  nickname?: string
): Promise<CreateRoomResult> {
  return sdk.rooms.join(roomCode, { nickname }, requireAuthToken());
}

// 获取房间信息（房间码预览）
export async function getRoomInfo(roomCode: string): Promise<RoomPreview> {
  return sdk.rooms.getInfo(roomCode);
}

// 房主点击「开始游戏」，从大厅推进到背景介绍——访客端轮询这个标记自动跟进
export async function startStory(roomId: string): Promise<void> {
  await sdk.rooms.startStory(roomId, requireReconnectToken());
}

// 房主在大厅加一个 AI 队友补位（exec/21）。它落座就带一张合法角色卡、状态即
// 「已就绪」，所以不会挡住开始游戏；只能在开局前加。
export async function addAiPlayer(roomId: string): Promise<RoomPlayerSummary> {
  return sdk.rooms.addAiPlayer(roomId, requireReconnectToken());
}

// 我的房间列表——用于「浏览已有游戏」入口。issue #106 起按**账号**返回该用户
// 参与过的全部房间（换台设备登录同一账号也能看到），不再是「这个浏览器的最后
// 一个房间」。没登录时返回空列表而不是报错：这个入口在未登录状态下也会被渲染。
export async function listMyRooms(): Promise<MyRoomSummary[]> {
  const token = getAuthToken();
  if (!token) return [];
  return sdk.rooms.listMyRooms(token);
}

/**
 * 房主彻底删除房间：房间、事件流、角色卡、复盘一起没，**不可撤回**。
 *
 * 跟「结束游戏 / 解散」是两件事——那两条只把房间标成已完成，复盘照常打得开。
 * 走账号 token：入口在「我的房间」，那里没有这个房间的重连凭证。
 */
export async function deleteRoom(roomId: string): Promise<void> {
  const token = getAuthToken();
  if (!token) throw new Error('未登录，无法删除房间');
  await sdk.rooms.remove(roomId, token);
}

// 房主结束游戏，房间转入「已完成」状态，之后只能查看复盘
export async function endGame(roomId: string): Promise<void> {
  await sdk.rooms.endGame(roomId, requireReconnectToken());
}

// ── 房间成员管理（2026-08-12）──────────────────────────────

// 把某个人移出大厅：房主踢别人，或者**本人自己退出**（大厅的「离开房间」）。
// **只在大厅阶段**——开局之后想把人赶走是社交问题，不是软件问题（后端也挡）。
export async function kickPlayer(roomId: string, playerId: string): Promise<void> {
  await sdk.rooms.kickPlayer(roomId, playerId, requireReconnectToken());
}

// 转让房主。不限阶段：真实场景恰恰是开局之后房主要先走。
export async function transferHost(roomId: string, playerId: string): Promise<void> {
  await sdk.rooms.transferHost(roomId, playerId, requireReconnectToken());
}

// 改人数上限。下界是当前人数（后端裁定），中途加入撞上"位置不够"时用它。
//
// `allowManualRolls` 不传 = 不动它（`exec/46` B5）：调一下人数不该顺手把
// 「骰子在桌上」关掉。
export async function updateRoomSettings(
  roomId: string,
  maxPlayers: number,
  allowManualRolls?: boolean
): Promise<void> {
  await sdk.rooms.updateSettings(roomId, maxPlayers, requireReconnectToken(), allowManualRolls);
}

// 房主解散房间。跟 endGame 的区别只有阶段：那条要求进行中，这条是"人没凑齐，
// 散了"。两者都不删数据，回放照常打得开。
export async function disbandRoom(roomId: string): Promise<void> {
  await sdk.rooms.disband(roomId, requireReconnectToken());
}

// 中途离开 / 回来。他的角色暂时退出剧情：不进守秘人的在场名单，守秘人下一段
// 会给一个说得通的理由把他送出这一幕。本人或房主可操作。
export async function setPlayerAway(
  roomId: string,
  playerId: string,
  away: boolean
): Promise<void> {
  await sdk.rooms.setPlayerAway(roomId, playerId, away, requireReconnectToken());
}

// 复盘摘要。highlights 是代码算的数字，summaryText 是模型写的一段回顾
// （没配 key 时为 null——那是如实的降级，前端要照实处理，不要编一段占位文案）。
export async function getRoomSummary(roomId: string): Promise<RoomSummary> {
  return sdk.rooms.getSummary(roomId, requireReconnectToken());
}
