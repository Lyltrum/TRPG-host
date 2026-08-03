import type { GameManifest } from '@/types/game'
import type { Scenario } from '@/types/game'

export const GAME_REGISTRY: GameManifest[] = [
  {
    id: 'trpg',
    name: '跑团',
    icon: 'scroll-text',
    description: '经典 TRPG 体验\n支持多规则系统',
    color: 'ink-blue',
    borderColor: 'border-ink-blue',
    iconBg: 'bg-[#eef3f8]',
    iconColor: 'text-ink-blue',
    status: 'recommended',
    // 摊平「选择世界」那一层之后（见 GameSelectionPage），这两条介绍从那个
    // 页面里的局部常量搬到了配置里——它们是数据，不是某一屏的排版。
    systems: [
      {
        id: 'coc',
        name: '克苏鲁的呼唤',
        nameEn: 'Call of Cthulhu 7th',
        description: '1920 年代调查员\n对抗宇宙恐怖的经典规则',
        status: 'ready',
      },
      {
        id: 'dnd',
        name: '龙与地下城',
        nameEn: 'Dungeons & Dragons 5e',
        description: '剑与魔法的奇幻冒险\n史诗级英雄传说',
        status: 'wip',
      },
    ],
  },
  {
    id: 'blood-clock',
    name: '血染钟楼',
    icon: 'clock',
    description: '社交推理\n找出恶魔与爪牙',
    color: 'rose',
    borderColor: 'border-[#8a4070]',
    iconBg: 'bg-[#f5eef4]',
    iconColor: 'text-[#8a4070]',
    status: 'coming-soon',
  },
  {
    id: 'werewolf',
    name: '狼人杀',
    icon: 'wolf',
    description: '经典发言推理\n谁是潜伏的狼人',
    color: 'rust',
    borderColor: 'border-[#c04040]',
    iconBg: 'bg-[#f8eeee]',
    iconColor: 'text-[#c04040]',
    status: 'coming-soon',
  },
  {
    id: 'script-murder',
    name: '剧本杀',
    icon: 'theater',
    description: '沉浸式剧情推演\n扮演你的角色',
    color: 'brown',
    borderColor: 'border-[#6a6050]',
    iconBg: 'bg-[#f2f0ec]',
    iconColor: 'text-[#6a6050]',
    status: 'coming-soon',
  },
]

export const GAME_COLORS: Record<string, { border: string; iconBg: string; iconColor: string }> = {
  'trpg': { border: 'border-ink-blue', iconBg: 'bg-[#eef3f8]', iconColor: 'text-ink-blue' },
  'blood-clock': { border: 'border-[#8a4070]', iconBg: 'bg-[#f5eef4]', iconColor: 'text-[#8a4070]' },
  'werewolf': { border: 'border-[#c04040]', iconBg: 'bg-[#f8eeee]', iconColor: 'text-[#c04040]' },
  'script-murder': { border: 'border-[#6a6050]', iconBg: 'bg-[#f2f0ec]', iconColor: 'text-[#6a6050]' },
}

export const SYSTEM_COLORS: Record<string, { border: string; iconBg: string; iconColor: string; name: string }> = {
  'coc': { border: 'border-[#7050a0]', iconBg: 'bg-[#f3eef8]', iconColor: 'text-[#7050a0]', name: '克苏鲁的呼唤 7th' },
  'dnd': { border: 'border-[#c08050]', iconBg: 'bg-[#f8f2ec]', iconColor: 'text-[#c08050]', name: '龙与地下城 5e' },
}

export function getGameById(id: string): GameManifest | undefined {
  return GAME_REGISTRY.find(g => g.id === id)
}

/**
 * 前端「选择模组」列表 = 契约发现实验的可玩模组全集。
 *
 * id 必须与后端 `app/core/keeper/contract/catalog.py` 的 scenario_id **完全一致**，
 * 建房时 `selectModule(roomId, sceneId)` 才能命中 DB，Keeper 加载对应
 * `模组资料/*.structured.json`。
 *
 * 前情正文不写在这里：`/room/story` 走 `GET /modules/{id}` 的
 * `storyPages`（structured 的 player_intro + opening.script）。
 */
export const SCENARIO_REGISTRY: Scenario[] = [
  {
    id: '00000000-0000-0000-0000-000000000003',
    name: '追书人',
    nameEn: 'The Book-Hunter',
    systemId: 'coc',
    description: '线性调查向短模组。失踪与藏书线索，适合 1–2 人试玩。',
    difficulty: '入门',
    status: 'ready',
    playerCount: '1-2 人',
    estimatedTime: '2-3 小时',
    storyLabel: '可玩 · 追书人',
    subtitle: 'BOOK-HUNTER',
  },
  {
    id: '00000000-0000-0000-0000-000000000004',
    name: '科比特先生',
    nameEn: 'Mister Corbitt',
    systemId: 'coc',
    description: '宅邸调查向。邻居异常、报纸线索与宅邸探索。',
    difficulty: '进阶',
    status: 'ready',
    playerCount: '1-4 人',
    estimatedTime: '3-5 小时',
    storyLabel: '可玩 · 科比特先生',
    subtitle: 'MISTER CORBITT',
  },
  {
    id: '00000000-0000-0000-0000-000000000005',
    name: '神秘渡轮',
    nameEn: 'The Ferry',
    systemId: 'coc',
    description: '封闭空间 + 倒计时压力。船上调查与时间窗口。',
    difficulty: '进阶',
    status: 'ready',
    playerCount: '1-4 人',
    estimatedTime: '3-4 小时',
    storyLabel: '可玩 · 神秘渡轮',
    subtitle: 'THE FERRY',
  },
  {
    id: '00000000-0000-0000-0000-000000000006',
    name: '复足',
    nameEn: 'Fuzu',
    systemId: 'coc',
    description: '封闭生存/战斗向。资源与威胁并重。',
    difficulty: '挑战',
    status: 'ready',
    playerCount: '1-4 人',
    estimatedTime: '3-5 小时',
    storyLabel: '可玩 · 复足',
    subtitle: 'FUZU',
  },
  {
    id: '00000000-0000-0000-0000-000000000007',
    name: '死者的顿足舞',
    nameEn: "Dead Man's Stomp",
    systemId: 'coc',
    description: '城市多线调查。篇幅较长，适合完整局压测。',
    difficulty: '进阶',
    status: 'ready',
    playerCount: '1-4 人',
    estimatedTime: '4-6 小时',
    storyLabel: '可玩 · 死者的顿足舞',
    subtitle: "DEAD MAN'S STOMP",
  },
]

export function getScenariosBySystem(systemId: string): Scenario[] {
  return SCENARIO_REGISTRY.filter(s => s.systemId === systemId)
}

export function getScenarioById(id: string): Scenario | undefined {
  return SCENARIO_REGISTRY.find(s => s.id === id)
}
