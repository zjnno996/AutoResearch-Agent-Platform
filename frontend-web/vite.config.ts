import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const resourcePort = process.env.RESOURCE_MONITOR_PORT || '8905'
const agentPort = process.env.AGENT_BRIDGE_PORT || '8906'
const reviewPort = process.env.REVIEW_PORT || '8907'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5910,
    proxy: {
      '/ws/resources': {
        target: `ws://localhost:${resourcePort}`,
        ws: true,
        rewriteWsOrigin: true,
        rewrite: () => '/',
      },
      '/ws/agents': {
        target: `ws://localhost:${agentPort}`,
        ws: true,
        rewriteWsOrigin: true,
        rewrite: () => '/',
      },
      '/download': {
        target: `http://localhost:${agentPort}`,
        changeOrigin: true,
      },
      '/api/review': {
        target: `http://localhost:${reviewPort}`,
        changeOrigin: true,
      },
      '/api/models': {
        target: `http://localhost:${reviewPort}`,
        changeOrigin: true,
      },
    },
  },
})
