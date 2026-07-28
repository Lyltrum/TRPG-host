/** 调查员基本信息 */
export interface InvestigatorInfo {
  name: string
  playerName: string
  age: string
  gender: string
  residence: string
  birthplace: string
  occupationId: number | null
}

/**
 * 属性值表：键用后端 ruleset 里的属性键（`STR`/`CON`/…/`LUCK`），值是属性值。
 *
 * 这里刻意**不**枚举有哪些属性、也不带默认值和中文标签——那些全都是规则数据，
 * 由后端 `GET /systems/{systemId}/ruleset` 提供（issue #96）：
 * - 有哪些属性、哪些能用点数购买 → `ruleset.attributes` 的 `key` / `pointBuy`
 * - 中文名 → `AttributeSpec.label`（缩写直接用 `key`）
 * - 默认值 / 点数预算 / 单项上下限 → `ruleset.attributePointBuy`
 *
 * 此前这些在前端硬编码了好几份（三处属性键名单 + 标签表 + 默认值 + 480 和
 * [10,90]），加一项属性要改好几个地方，漏一处就静默出错——PR #88 加幸运时
 * 就漏了角色卡视图那处，导致建好的卡看不到幸运值。
 *
 * 键名也直接用后端的大写形式，不再前端小写、提交时转换一次：那层转换本身
 * 就是两套命名并存的产物，去掉之后少一类对不上的可能。
 */
export type Attributes = Record<string, number>

/**
 * 属性生成方式（迁移自 coc-char-gen，character-build-migration）。值必须跟
 * 后端 `coc7_rules.py` 的字符串常量完全一致：
 * - `pointbuy`：点数购买法（默认，本项目原有唯一路径）。
 * - `roll`：服务端权威掷骰，8 项属性直接掷定，不能手动分配。
 * - `roll_pool`：掷点池——服务端掷出一个总点数池，玩家手动分配到八维。
 */
export type GenerationMethod = 'pointbuy' | 'roll' | 'roll_pool'

/**
 * 结构化背景故事（迁移自 coc-char-gen）。跟 `Attributes` 不同，这 8 个字段
 * 是这次产品设计定下来的固定字段（不是规则数据、不会随规则系统变化），所以
 * 用固定字段的 interface 而不是开放的 Record。
 *
 * 后端 `background_detail` 是不做逐键校验的透明 `dict[str, str]`（键的含义
 * 只有前端知道），这里的键名直接就是发给后端的键名。
 */
export interface BackgroundDetail {
  personalDescription: string
  ideology: string
  significantPeople: string
  meaningfulLocations: string
  treasuredPossessions: string
  traits: string
  injuries: string
  phobias: string
}

export function emptyBackgroundDetail(): BackgroundDetail {
  return {
    personalDescription: '',
    ideology: '',
    significantPeople: '',
    meaningfulLocations: '',
    treasuredPossessions: '',
    traits: '',
    injuries: '',
    phobias: '',
  }
}

/**
 * 结构化背景故事 8 个字段的中文标签 + 填写提示，建卡向导（CharacterPage）
 * 和游戏内角色卡（RoomPage）两处都要用同一份——两处各写一份的话，以后加/改
 * 字段容易漏改一处，正是 issue #96 属性名单重复维护踩过的坑。
 */
export const BACKGROUND_DETAIL_FIELDS: Array<{ key: keyof BackgroundDetail; label: string; placeholder: string }> = [
  { key: 'personalDescription', label: '个人描述', placeholder: '外貌、习惯、说话方式…' },
  { key: 'ideology', label: '信念 / 思想', placeholder: '角色的信仰、价值观、人生哲学…' },
  { key: 'significantPeople', label: '重要之人', placeholder: '对角色而言最重要的人是谁，为什么…' },
  { key: 'meaningfulLocations', label: '意义非凡的地点', placeholder: '角色心中特殊的地方…' },
  { key: 'treasuredPossessions', label: '珍视的物品', placeholder: '角色随身携带或格外珍惜的东西…' },
  { key: 'traits', label: '特质', placeholder: '性格特点、怪癖…' },
  { key: 'injuries', label: '外伤', placeholder: '角色身上留下的伤痕或旧疾…' },
  { key: 'phobias', label: '恐惧症', placeholder: '角色特有的恐惧或心理阴影…' },
]
