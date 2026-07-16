// ESLint 9 flat config。
//
// 接入 typescript-eslint 的 recommended 规则集 + eslint-plugin-react-hooks
// 的 recommended 规则集。不用 strictTypeChecked / stylisticTypeChecked ——
// 见 issue #73 决策 3：实测会带来 196~245 处存量违规，而前端目前零测试、
// 没有回归防线，这种规模的机械改动风险不成比例，等有测试再上。
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'

export default tseslint.config(
  {
    // dist 是构建产物，node_modules 不属于本包源码，都不需要 lint。
    ignores: ['dist/**'],
  },
  ...tseslint.configs.recommended,
  reactHooks.configs['recommended-latest'],
)
