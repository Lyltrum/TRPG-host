/** 品牌标志：**主持人屏风**（KP/DM 挡板 + 骰面）。
 *
 * 🔴 内联 SVG 而不是 `<img src="/logo.png">`：
 * - 它要跟着容器换色（纸板底上是墨色、印章里是米色），`currentColor` 才做得到；
 * - 原来那张是 **237KB 的 JPEG**（1080×1080，边缘带压缩噪点），在 62px 的品牌区
 *   和 24px 的标签页上都糊；
 * - 它画了十来个元素（DM 字母 + 芯片 + 地图 + 骰子 + 棋子 + 卡牌），缩小后认不出。
 *   判据换成一条：**24px 还认得出**。
 *
 * favicon 是同一枚图形的独立文件（`public/favicon.svg`，带深底方块）。两处都改
 * 才算换完 —— 只换一处的话浏览器标签页还是旧的。
 */
export default function BrandMark({ className = '' }: { className?: string }) {
  return (
    <svg viewBox="0 0 100 100" className={className} aria-hidden="true">
      <g fill="none" stroke="currentColor" strokeWidth={7} strokeLinejoin="round">
        <path d="M32 26 h36 v50 h-36 z" />
        <path d="M32 26 L11 33 v50 l21 -7" />
        <path d="M68 26 L89 33 v50 l-21 -7" />
      </g>
      <g fill="currentColor">
        <circle cx="42" cy="40" r="4.4" />
        <circle cx="58" cy="40" r="4.4" />
        <circle cx="50" cy="51" r="4.4" />
        <circle cx="42" cy="62" r="4.4" />
        <circle cx="58" cy="62" r="4.4" />
      </g>
    </svg>
  )
}
