import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [
    react(),
    // Electron loads from file:// — the crossorigin attribute Vite adds to
    // <script type="module"> tags triggers a CORS check that fails because
    // file:// has no HTTP headers. Strip it from the built HTML.
    {
      name: 'remove-crossorigin',
      transformIndexHtml(html) {
        return html.replace(/\s+crossorigin(?:="[^"]*")?/g, '');
      },
    },
  ],
  root: 'renderer',
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    strictPort: true,
  },
});
