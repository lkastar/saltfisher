import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // The token lives in localStorage and rides as a Bearer header, so dev and
    // prod both hit relative /api paths: no VITE_API_URL to keep in sync and
    // no CORS config on the backend.
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
