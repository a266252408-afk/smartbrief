import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  // 生成 manifest 供首屏体积对照；不改变产物代码。
  build: {
    manifest: true
  },
  server: {
    host: '0.0.0.0',
    port: 5174,
    // 将 /api 请求代理到后端，避免跨域
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8002',
        changeOrigin: true
      }
    }
  }
})
