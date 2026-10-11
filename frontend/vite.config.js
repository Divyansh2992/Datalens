import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API base URL is chosen at runtime in src/api/client.js
// (localhost -> local backend, deployed site -> Render backend), so no dev proxy is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
})
