import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发期把 /api 代理到后端 FastAPI（:8000），前端用相对路径调用，同源免 CORS。
// 生产构建产物 dist/ 可另行部署，届时按需配反向代理或后端 StaticFiles 挂载。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
