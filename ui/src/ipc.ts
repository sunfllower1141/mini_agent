/**
 * ipc.ts ÃÃÃÂ¢ JSON-line IPC client matching headless_ipc.py.
 *
 * Spawns the Python backend as a child process, reads its stdout line by
 * line (one JSON event per line), and writes commands back on stdin.  Events
 * are dispatched to subscribers registered with onEvent().
 *
 * Keep these constants in sync with headless_ipc.py.
 */
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {createInterface} from 'node:readline';

// ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢
// Event types (PythonÃÃÂ¢UI) ÃÃÃÂ¢ must match headless_ipc.EVT_* constants
// ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢ÃÃÃÂ¢
export const EVT = {
  READY:          'ready',
  STREAM_TOKEN:   'stream.token',
  STREAM_THINK:   'stream.thinking',
  TOOL_START:     'tool.start',
  TOOL_END:       'tool.end',
  TOOL_OUTPUT:    'tool.output',
  SUBAGENT_SPAWN: 'subagent.spawn',
  SUBAGENT_TOKEN: 'subagent.token',
  SUBAGENT_DONE:  'subagent.done',
  TURN_DONE:      'turn.done',
  APPROVE_REQ:    'approve.request',
  ERROR:          'error',
  STATUS:         'status',
  LOG:            'log',
} as const;

export const CMD = {
  USER_MESSAGE: 'user.message',
  USER_CANCEL:  'user.cancel',
  USER_APPROVE: 'user.approve',
  USER_COMMAND: 'user.command',
  USER_QUIT:    'user.quit',
} as const;

export type EventType   = (typeof EVT)[keyof typeof EVT];
export type CommandType = (typeof CMD)[keyof typeof CMD];

export interface IpcEvent {
  type: EventType | string;
  data: Record<string, any>;
  ts:   number;
}

export type EventHandler = (evt: IpcEvent) => void;

/**
 * Spawns the Python headless backend and bridges its JSON-line stream.
 *
 *   const ipc = new IpcClient(['python', 'mini_agent_headless.py']);
 *   ipc.onEvent(e => ...);
 *   ipc.send(CMD.USER_MESSAGE, {text: 'hi'});
 */
export class IpcClient {
  private child: ChildProcessWithoutNullStreams;
  private handlers: Set<EventHandler> = new Set();
  private closed = false;

  constructor(command: string[], opts: {cwd?: string; env?: NodeJS.ProcessEnv} = {}) {
    if (command.length === 0) throw new Error('IpcClient: empty command');
    const [bin, ...args] = command;
    this.child = spawn(bin as string, args, {
      cwd: opts.cwd,
      env: {...process.env, ...(opts.env ?? {})},
      stdio: ['pipe', 'pipe', 'pipe'],
    }) as ChildProcessWithoutNullStreams;

    const rl = createInterface({input: this.child.stdout});
    rl.on('line', (line) => this.handleLine(line));

    // Surface backend stderr as 'log' events so the UI can show errors.
    const errRl = createInterface({input: this.child.stderr});
    errRl.on('line', (line) => {
      if (!line) return;
      this.dispatch({type: EVT.LOG, data: {level: 'stderr', msg: line}, ts: Date.now() / 1000});
    });

    this.child.on('exit', (code, signal) => {
      this.closed = true;
      this.dispatch({
        type: EVT.LOG,
        data: {level: 'info', msg: `backend exited (code=${code ?? signal})`},
        ts: Date.now() / 1000,
      });
    });
    this.child.on('error', (err) => {
      this.dispatch({type: EVT.ERROR, data: {msg: `spawn failed: ${err.message}`}, ts: Date.now() / 1000});
    });
  }

  private handleLine(line: string): void {
    if (!line.trim()) return;
    let evt: IpcEvent;
    try {
      evt = JSON.parse(line) as IpcEvent;
    } catch (e) {
      this.dispatch({
        type: EVT.LOG,
        data: {level: 'warn', msg: `bad JSON from backend: ${line.slice(0, 200)}`},
        ts: Date.now() / 1000,
      });
      return;
    }
    if (typeof evt.type !== 'string') return;
    if (typeof evt.data !== 'object' || evt.data == null) evt.data = {};
    this.dispatch(evt);
  }

  private dispatch(evt: IpcEvent): void {
    for (const h of this.handlers) {
      try { h(evt); } catch { /* never let a UI bug kill the bridge */ }
    }
  }

  /** Subscribe.  Returns an unsubscribe callback. */
  onEvent(h: EventHandler): () => void {
    this.handlers.add(h);
    return () => { this.handlers.delete(h); };
  }

  /** Send a command to the backend.  Best-effort; silently drops if closed. */
  send(type: CommandType | string, data: Record<string, any> = {}): void {
    if (this.closed) return;
    const line = JSON.stringify({type, data}) + '\n';
    try {
      this.child.stdin.write(line);
    } catch {
      this.closed = true;
    }
  }

  /** Tell the backend to quit, then kill if it lingers. */
  shutdown(timeoutMs = 2000): void {
    if (this.closed) return;
    this.send(CMD.USER_QUIT);
    try { this.child.stdin.end(); } catch { /* ignore */ }
    const t = setTimeout(() => {
      try { this.child.kill('SIGTERM'); } catch { /* ignore */ }
    }, timeoutMs);
    this.child.once('exit', () => clearTimeout(t));
  }
}
