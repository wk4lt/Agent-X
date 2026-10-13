import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const backendPort = env.BACKEND_PORT || "8000";
  return {
    plugins: [react()],
    server: {
      strictPort: true,
      proxy: { "/api": `http://127.0.0.1:${backendPort}` },
    },
  };
});
