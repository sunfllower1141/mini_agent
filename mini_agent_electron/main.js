/**
 * main.js -- Bootstrap loader for main.ts.
 *
 * Uses tsx to register TypeScript transpilation, then requires main.ts.
 * This is a thin shim so Electron's "main" field in package.json works.
 */
require('tsx/cjs');
require('./main.ts');
