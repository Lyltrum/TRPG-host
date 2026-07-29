import { defineConfig, mergeConfig } from 'vitest/config';
import viteConfig from './vite.config';

// 建卡向导 bug 修复第二轮（wizard-bugfix-round2.md）新增：这个仓库此前没有
// 任何前端单测基础设施，这里是第一份。复用 vite.config.ts（同一套 `@` 别名
// /React 插件），只叠加 `test` 配置，不建两套平行的构建配置。
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
    },
  })
);
