import { fileURLToPath } from 'url'
import { dirname, resolve } from 'path'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

export default defineConfig({
  plugins: [tailwindcss()],
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        episode: resolve(__dirname, 'episode.html'),
        upload: resolve(__dirname, 'upload.html'),
        vocab: resolve(__dirname, 'vocab.html'),
      },
    },
  },
})
