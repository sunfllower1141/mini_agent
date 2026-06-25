/**
 * preload.js -- Bootstrap loader for preload.ts.
 *
 * Uses tsx to register TypeScript transpilation, then requires preload.ts.
 * This is a thin shim so Electron's preload script path works.
 */
require('tsx/cjs');
require('./preload.ts');
