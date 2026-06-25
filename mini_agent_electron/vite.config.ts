import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [
    react(),
    // Electron loads from file:// -- the crossorigin attribute Vite adds to
    // <script type="module"> tags triggers a CORS check that fails because
    // file:// has no HTTP headers. Strip it from the built HTML.
    {
      name: 'remove-crossorigin',
      transformIndexHtml(html) {
        return html.replace(/\s+crossorigin(?:="[^"]*")?/g, '');
      },
    },
    // Ensure CSS <link> tags appear after inline <script> blocks but
    // before external/module <script> tags. This lets the theme inline
    // script set data-theme before CSS loads, while ensuring CSS is ready
    // before React's module script executes.
    {
      name: 'css-before-module-scripts',
      enforce: 'post',
      transformIndexHtml(html) {
        const linkRegex = /<link\s+rel=["']stylesheet["'][^>]*>/gi;
        const links = [];
        let cleaned = html.replace(linkRegex, (match) => {
          links.push(match);
          return '';
        });
        if (links.length === 0) return cleaned;
        // Find the first <script> that has a src= or type="module" (external/module)
        const externalScriptRe = /<script\s(?=[^>]*\b(?:src=|type=["']module["']))[^>]*>/i;
        const match = externalScriptRe.exec(cleaned);
        if (match) {
          const idx = match.index;
          cleaned = cleaned.slice(0, idx) + links.join('\n  ') + '\n  ' + cleaned.slice(idx);
        } else {
          // No external script — append before </head>
          const headEnd = cleaned.indexOf('</head>');
          if (headEnd !== -1) {
            cleaned = cleaned.slice(0, headEnd) + '\n  ' + links.join('\n  ') + '\n' + cleaned.slice(headEnd);
          }
        }
        return cleaned;
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
