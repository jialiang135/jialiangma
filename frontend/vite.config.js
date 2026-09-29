import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发服务器把 /api 代理到后端。
// 默认 7860 = 「python main.py」的本地端口（config/settings.py 的 port）。
// 原先默认 7863（那是 docker-compose 里的容器端口），导致本地 npm run dev 时
// 所有 /api 请求被代理到一个空端口 → 连接被拒 → 页面一片报错，
// 而实际上后端是好的、只是开发服务器指错了地方。
// 后端跑在 Docker 里时这样用：VITE_API_PORT=7863 npm run dev
const API_PORT = process.env.VITE_API_PORT || '7860';

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${API_PORT}`,
        changeOrigin: true,
        // 语音合成的 WebSocket 也走 /api/tts/stream，必须显式开启 ws 代理，
        // 否则开发模式下握手会被当成普通 HTTP 请求而失败
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
  },
})
