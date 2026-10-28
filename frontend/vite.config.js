import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the dashboard calls /api on its own origin and Vite forwards it
// to FastAPI, so the browser never needs CORS. Override with TESTPILOT_API_PROXY.
const apiTarget = process.env.TESTPILOT_API_PROXY || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: apiTarget, changeOrigin: false } } },
  preview: { proxy: { "/api": { target: apiTarget, changeOrigin: false } } },
});
