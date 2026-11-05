import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址默认本机 8000；要连测试库（另起一个 uvicorn 指向库副本）时
// 用 API_TARGET=http://127.0.0.1:8011 起前端即可，不必改这个文件——
// 否则每次「看一眼测试数据」都要先改配置再改回来，早晚有人忘了改回去。
const API_TARGET = process.env.API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
      '/media': { target: API_TARGET, changeOrigin: true }
    }
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 1500 }
})
