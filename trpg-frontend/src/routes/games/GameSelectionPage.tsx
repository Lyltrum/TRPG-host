import { useNavigate } from 'react-router-dom'
import { ScrollText, Clock, Ghost, Theater, Shield, Swords } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { GAME_REGISTRY, SYSTEM_COLORS } from '@/config/games'
import Badge from '@/shared/components/Badge'
import { useGameStore } from '@/stores/game-store'

const iconMap: Record<string, LucideIcon> = {
  'scroll-text': ScrollText,
  'clock': Clock,
  'wolf': Ghost,
  'theater': Theater,
  'shield': Shield,
  'swords': Swords,
}

const SYSTEM_ICONS: Record<string, string> = { coc: 'shield', dnd: 'swords' }

function getStatusBadge(status: string) {
  switch (status) {
    case 'recommended':
      return <Badge variant="success">推荐</Badge>
    case 'coming-soon':
      return <Badge variant="default">开发中</Badge>
    case 'wip':
      return <Badge variant="default">开发中</Badge>
    default:
      return null
  }
}

/** 🔴 这一屏是**摊平**的：`跑团` 那一层去掉了。
 *
 * 原来是「选择游戏 → 选择世界 → 选模组」三层，而中间那层永远只有两项
 * （COC7 可玩 / DND 开发中）——多点一次，却没提供任何新信息。现在带规则系统
 * 的游戏把每个系统摊成一张卡，点可玩的那张直接进选模组。
 *
 * 没有 `systems` 的游戏（血染钟楼 / 狼人杀 / 剧本杀）本来就没有这一层，
 * 原样一张卡。`systemId` 为空 = 还不能玩，点了不跳转。
 */
const ENTRIES = GAME_REGISTRY.flatMap((game) =>
  game.systems
    ? game.systems.map((sys) => ({
        key: `${game.id}:${sys.id}`,
        gameId: game.id,
        systemId: sys.status === 'ready' ? sys.id : null,
        name: sys.name,
        description: sys.description,
        icon: SYSTEM_ICONS[sys.id] ?? game.icon,
        iconBg: SYSTEM_COLORS[sys.id]?.iconBg ?? game.iconBg,
        iconColor: SYSTEM_COLORS[sys.id]?.iconColor ?? game.iconColor,
        borderColor: SYSTEM_COLORS[sys.id]?.border ?? game.borderColor,
        status: sys.status === 'ready' ? 'recommended' : 'wip',
      }))
    : [
        {
          key: game.id,
          gameId: game.id,
          systemId: null,
          name: game.name,
          description: game.description,
          icon: game.icon,
          iconBg: game.iconBg,
          iconColor: game.iconColor,
          borderColor: game.borderColor,
          status: game.status,
        },
      ]
)

export default function GameSelectionPage() {
  const navigate = useNavigate()
  // ★ 只有从"创建房间→选择游戏"这条子流程进来（returnFromGameSelect）才允许
  // 继续往下选模组/建卡；从登录页"浏览已有游戏"直接进来的只能看，不能往下走
  // ——建卡必须绑定一个真实房间（见需求：浏览入口不应该能进入游戏流程）。
  // 🔴 这道闸门原本在「选择世界」那一屏上，摊平时必须跟着搬过来，否则浏览
  // 模式的人会直接走进选模组。
  const canProceed = useGameStore((s) => s.returnFromGameSelect)

  return (
    <div className="animate-screen-in">
      <div className="flex items-center gap-2.5 px-5 pb-3 pt-1">
        <button
          onClick={() => navigate('/home')}
          className="w-[34px] h-[34px] rounded-full bg-card border border-border-light flex items-center justify-center flex-shrink-0 active:bg-panel active:scale-[0.94] transition-all duration-150"
        >
          <svg className="w-[18px] h-[18px] text-text-muted" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <h2 className="text-lg font-bold text-text-primary">选择游戏</h2>
      </div>

      {!canProceed && (
        <div className="mx-5 mb-4 px-3.5 py-2.5 bg-[#fdf3e0] border border-[#e0c088] rounded-[6px] text-[12px] text-[#8a6a2a]">
          浏览模式：创建或加入房间后才能继续选择模组、创建角色
        </div>
      )}

      <div className="px-5 grid grid-cols-2 gap-3">
        {ENTRIES.map((entry) => {
          const IconComp = iconMap[entry.icon] || ScrollText
          const playable = entry.systemId != null && canProceed
          return (
            <div
              key={entry.key}
              onClick={() => {
                if (playable) navigate(`/home/create/games/${entry.gameId}/scenarios/${entry.systemId}`)
              }}
              className={`
                bg-card border border-border-light rounded-md p-[22px] text-center
                transition-all duration-200 relative border-b-[3px] ${entry.borderColor}
                ${playable ? 'cursor-pointer active:scale-[0.96]' : 'opacity-60'}
              `}
            >
              <div className={`w-[52px] h-[52px] rounded-[14px] mx-auto mb-2.5 flex items-center justify-center ${entry.iconBg}`}>
                <IconComp className={`w-[26px] h-[26px] ${entry.iconColor}`} />
              </div>
              <div className="text-sm font-semibold text-text-primary mb-0.5">{entry.name}</div>
              <div className="text-[11px] text-text-muted leading-[1.4] whitespace-pre-line">{entry.description}</div>
              <div className="mt-2">{getStatusBadge(entry.status)}</div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
