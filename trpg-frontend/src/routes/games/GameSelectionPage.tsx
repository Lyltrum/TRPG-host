import { useNavigate } from 'react-router-dom'
import { ScrollText, Clock, Ghost, Theater, Shield, Swords } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { GAME_REGISTRY } from '@/config/games'
import Badge from '@/shared/components/Badge'
import { useGameStore } from '@/stores/game-store'
import ShellPage from '@/shared/components/ShellPage'

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
/** 每款游戏 / 规则系统的索引色。纸板上靠**实色**区分，不靠浅底色块——
 *  浅底压在纸板上几乎看不出来（这是换 theme-shell 时踩到的同一条）。 */
const ACCENT: Record<string, string> = {
  coc: '#2f6d8c',
  dnd: '#c9822f',
  'blood-clock': '#8a4070',
  werewolf: '#c9452f',
  'script-murder': '#6a6050',
}

const ENTRIES = GAME_REGISTRY.flatMap((game) =>
  game.systems
    ? game.systems.map((sys) => ({
        key: `${game.id}:${sys.id}`,
        gameId: game.id,
        systemId: sys.status === 'ready' ? sys.id : null,
        name: sys.name,
        description: sys.description,
        icon: SYSTEM_ICONS[sys.id] ?? game.icon,
        accent: ACCENT[sys.id] ?? '#5c5347',
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
          accent: ACCENT[game.id] ?? '#5c5347',
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
    <ShellPage title="选择游戏" onBack={() => navigate('/home')}>
      {!canProceed && (
        <div className="mx-5 mb-4 px-3 py-2 border-2 border-text-primary bg-card text-[11.5px] text-text-body leading-relaxed">
          浏览模式：创建或加入房间后才能继续选择模组、创建角色
        </div>
      )}

      {/* 每款游戏一张卡：顶边一道自己的色（配置里就带着），像盒子上的色标 */}
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
                press-soft bg-card p-4 pt-3 text-center transition-all duration-100
                ${playable ? 'cursor-pointer active:translate-x-[3px] active:translate-y-[3px] active:shadow-none' : 'opacity-65'}
              `}
              style={{ borderTopWidth: 6, borderTopColor: entry.accent }}
            >
              <div
                className="w-[46px] h-[46px] mx-auto mb-2 flex items-center justify-center border-2 border-text-primary"
                style={{ backgroundColor: entry.accent, color: '#fff5ea' }}
              >
                <IconComp className="w-[24px] h-[24px]" strokeWidth={2} />
              </div>
              <div className="text-[13.5px] font-extrabold text-text-primary mb-0.5">{entry.name}</div>
              <div className="text-[10.5px] text-text-muted leading-[1.5] whitespace-pre-line">{entry.description}</div>
              <div className="mt-2">{getStatusBadge(entry.status)}</div>
            </div>
          )
        })}
      </div>
    </ShellPage>
  )
}
