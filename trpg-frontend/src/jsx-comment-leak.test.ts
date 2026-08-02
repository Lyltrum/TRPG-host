/**
 * JSX 的 children 位置里不许写 `//` 注释——它会被**渲染成文本**。
 *
 * ## 为什么需要它
 *
 * 真机上出现过一行 `// 待掷 = 一张盖了章的表单，横排：…` 直接印在检定卡片
 * 上方给玩家看。写的时候当成注释写，实际上 JSX 里只有花括号星号那种才是注释，
 * `//` 在 children 位置就是一个普通文本节点。
 *
 * 🔴 **`tsc` 与 `eslint` 都不会报**：它语法完全合法，只是语义不是你以为的那个。
 * 官方能抓它的是 `react/jsx-no-comment-textnodes`，但那要引入整个
 * eslint-plugin-react —— 与 eslint.config.js 里「不轻易扩规则集」的既定决策
 * 冲突（issue #73 决策 3）。一条测试成本更低，也更符合本项目"约束要有测试
 * 守护"的习惯。
 *
 * ## 判据
 *
 * 一行 `//` 注释，如果**紧跟着一个 JSX 标签**，并且**前一行不以 `(` 结尾**
 * （以 `(` 结尾说明它处在 `return (` / `&& (` 这类表达式开始处，那里是 JS
 * 上下文，`//` 是真注释），就判为泄漏。
 *
 * ⚠️ 用 `import.meta.glob` 读源码，**不用 node:fs**：本包的 tsconfig 不含
 * node 类型，`tsc -b` 会直接报 TS2307 —— 我第一版就是这么把 build 弄挂的
 * （vitest 过了、build 没过，而我只看了前者）。
 */

import { describe, expect, it } from 'vitest'

const SOURCES = import.meta.glob('./**/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

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
    if (meaningful(i, 1).startsWith('<') && !meaningful(i, -1).endsWith('(')) {
      found.push({ line: i + 1, text: line.trim() })
    }
  })
  return found
}

describe('JSX 注释不会被渲染成文本', () => {
  it('扫描到了 tsx 文件（防止这条用例空转）', () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(10)
  })

  it.each(Object.entries(SOURCES))('%s', (_path, source) => {
    const found = leaks(source)
    expect(
      found,
      found
        .map((f) => `第 ${f.line} 行：${f.text}\n  → JSX children 里的 // 会被印在界面上，改用花括号星号注释`)
        .join('\n'),
    ).toEqual([])
  })

  it('判据本身有效（正例抓得到，反例不误伤）', () => {
    expect(leaks(['  <div>', '    // 这行会被印出来', '    <span />', '  </div>'].join('\n'))).toHaveLength(1)
    expect(leaks(['  return (', '    // 这行是真注释', '    <div />'].join('\n'))).toHaveLength(0)
  })
})
