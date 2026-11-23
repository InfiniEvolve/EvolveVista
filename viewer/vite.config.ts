// The campaign page. Its trajectory parts (the graph model, the model providers, the language switch, the
// styles) and the skill's references come from TraceVista, the submodule in third_party/, as source: an update
// of TraceVista reaches this page at the next build. The viewer an analysis opens in (trajectory.html) is
// TraceVista's own build, copied beside this page by scripts/build.mjs.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const root = fileURLToPath(new URL('.', import.meta.url));
export const TRACEVISTA = resolve(root, '../third_party/TraceVista');

export default defineConfig({
  root, base: './', publicDir: false,
  plugins: [react()],
  resolve: {
    alias: { '@tracevista-skill': resolve(TRACEVISTA, 'skills/tracevista-skill'), '@tracevista': resolve(TRACEVISTA, 'viewer/src/trajectory') },
    dedupe: ['react', 'react-dom', '@xyflow/react'],
  },
  server: { fs: { allow: [root, TRACEVISTA] } },
  build: { outDir: resolve(root, '../evolve_vista/static'), emptyOutDir: true,
    rollupOptions: { input: { campaign: resolve(root, 'campaign.html') } } },
});
