/**
 * JSX 的 children 位置里不许写 `//` 注释——它会被**渲染成文本**。
 *
 * ## 为什么需要它
 *
 * 真机上出现过一行 `// 待掷 = 一张盖了章的表单，横排：…` 直接印在检定卡片
 * 上方给玩家看。写的时候以为那是注释，实际上 JSX 里只有 `{＊ … ＊}` 是注释，
 * `//` 在 children 位置就是一个普通文本节点。
 *
 * 🔴 **`tsc` 与 `eslint` 都不会报**：它语法完全合法，只是语义不是你以为的那个。
 * 能抓到它的官方规则是 `react/jsx-no-comment-textnodes`，但那要引入整个
 * eslint-plugin-react —— 而本项目的 eslint 配置明确写了不轻易扩规则集
 * （见 eslint.config.js 顶部关于 issue #73 决策 3 的说明）。一条几十行的
 * 测试成本更低，也更符合本项目"架构约束必须有测试守护"的习惯。
 *
 * ## 判据
 *
 * 一行 `//` 注释，如果**紧跟着一个 JSX 标签**，并且**前一行不是以 `(` 结尾**
 * （以 `(` 结尾说明它处在 `return (` / `&& (` 这类表达式开始处，那里是 JS
 * 上下文，`//` 是真注释），就判为泄漏。
 */

import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'

// vitest 默认环境下 `import.meta.url` 可能不可用，用 __dirname 更稳；
// 两者都不在时退回相对路径（从包根跑）。
const SRC = typeof __dirname === 'string' ? __dirname : join(dirname('.'), 'src')

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) return tsxFiles(full)
    return name.endsWith('.tsx') ? [full] : []
  })
}

function leaks(source: string): { line: number; text: string }[] {
  const lines = source.split('\n')
  const meaningful = (i: number, step: -1 | 1): string => {
    let j = i + step
    while (j >= 0 && j < lines.length && (lines[j].trim() === '' || /^\s*\/\//.test(lines[j]))) {
      j += step
    }
    return j >= 0 && j < lines.length ? lines[j].trim() : ''
  }
  const found: { line: number; text: string }[] = []
  lines.forEach((line, i) => {
    if (!/^\s*\/\//.test(line)) return
    const next = meaningful(i, 1)
    const prev = meaningful(i, -1)
    if (next.startsWith('<') && !prev.endsWith('(')) {
      found.push({ line: i + 1, text: line.trim() })
    }
  })
  return found
}

describe('JSX 注释不会被渲染成文本', () => {
  it('扫描到了 tsx 文件（防止这条用例空转）', () => {
    expect(tsxFiles(SRC).length).toBeGreaterThan(10)
  })

  it.each(tsxFiles(SRC).map((f) => [f.slice(SRC.length), f] as const))(
    '%s',
    (_rel, file) => {
      const found = leaks(readFileSync(file, 'utf-8'))
      expect(
        found,
        found.map((f) => `第 ${f.line} 行：${f.text}\n  → JSX children 里的 // 会被印在界面上，改用 {/* */}`).join('\n'),
      ).toEqual([])
    },
  )

  it('判据本身是有效的（正例会被抓到，反例不会）', () => {
    // children 位置 → 应当抓到
    expect(leaks(['  <div>', '    // 这行会被印出来', '    <span />', '  </div>'].join('\n'))).toHaveLength(1)
    // 表达式开始处 → 是真注释，不该抓
    expect(leaks(['  return (', '    // 这行是真注释', '    <div />'].join('\n'))).toHaveLength(0)
  })
})
