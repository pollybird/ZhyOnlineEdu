import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/ · https://vitest.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      // 后端 Flask /api/v1（§15.1）
      '/api': { target: 'http://127.0.0.1:5000', changeOrigin: true },
    },
  },
  test: {
    // 只测纯逻辑（请求层判定、权限函数）：node 环境，不引入 jsdom
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
