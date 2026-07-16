import { defineConfig } from 'vite'
import { copyFile, mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const frontendDir = fileURLToPath(new URL('.', import.meta.url))
const monacoDir = fileURLToPath(new URL('./node_modules/monaco-editor/', import.meta.url))
const noticeOutputDir = fileURLToPath(
  new URL('../pages/template-editor/vendor/monaco/', import.meta.url),
)

function copyMonacoNotices() {
  return {
    name: 'copy-monaco-notices',
    async closeBundle() {
      await mkdir(noticeOutputDir, { recursive: true })
      await Promise.all(
        ['LICENSE', 'ThirdPartyNotices.txt'].map((name) =>
          copyFile(`${monacoDir}${name}`, `${noticeOutputDir}${name}`),
        ),
      )
    },
  }
}

export default defineConfig({
  root: frontendDir,
  base: './',
  plugins: [copyMonacoNotices()],
  build: {
    outDir: '../pages/template-editor',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2020',
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
})
