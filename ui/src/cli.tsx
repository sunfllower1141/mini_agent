/**
 * cli.tsx - Ink CLI entry point.
 *
 *   node ui/dist/cli.js [--python-cmd "python3"] [--theme slate] [--workspace PATH]
 *
 * Spawns mini_agent_headless.py as a child process and renders <App/>.
 * All other CLI args are forwarded to the Python backend so flags like
 * --stream / --approve / --unrestricted keep working unchanged.
 */
import React from 'react';
import {render} from 'ink';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

import {IpcClient} from './ipc.js';
import {App} from './components/App.js';

interface ParsedArgs {
  pythonCmd: string;
  theme?:    string;
  backendArgs: string[];
}

function parseArgs(argv: string[]): ParsedArgs {
  const out: ParsedArgs = {pythonCmd: process.env.MINI_AGENT_PYTHON ?? 'python3', backendArgs: []};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--python-cmd' && i + 1 < argv.length) {
      out.pythonCmd = argv[++i] ?? out.pythonCmd;
    } else if (a === '--theme' && i + 1 < argv.length) {
      out.theme = argv[++i];
    } else {
      out.backendArgs.push(a as string);
    }
  }
  return out;
}

function resolveBackendScript(): string {
  // dist/cli.js (ui/dist/) or src/cli.tsx (ui/src/)
  // -> ../../mini_agent_headless.py (project root)
  const here = path.dirname(fileURLToPath(import.meta.url));
  return path.resolve(here, '..', '..', 'mini_agent_headless.py');
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const script = process.env.MINI_AGENT_BACKEND ?? resolveBackendScript();
  const cmd = [args.pythonCmd, script, ...args.backendArgs];
  const ipc = new IpcClient(cmd, {env: {PYTHONUNBUFFERED: '1'}});

  const themeFromEnv = process.env.MINI_AGENT_THEME;
  const initialTheme = args.theme ?? themeFromEnv;

  const {waitUntilExit} = render(<App ipc={ipc} initialTheme={initialTheme} />);

  const shutdown = () => {
    ipc.shutdown();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  waitUntilExit().then(() => {
    ipc.shutdown();
    process.exit(0);
  });
}

main();
