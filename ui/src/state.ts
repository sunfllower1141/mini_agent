/**
 * state.ts ÃÃÃÂ¢ React reducer for chat/tool/agent state.
 *
 * Events arrive from ipc.ts and are translated into state transitions here.
 * The component layer subscribes via useReducer + a single dispatch.
 */
import type {IpcEvent} from './ipc.js';

export type ToolStatus = 'running' | 'ok' | 'fail';

export interface ToolCallState {
  seq:          number;
  agentId:      string;
  summary:      string;
  parallel:     boolean;
  status:       ToolStatus;
  detail?:      string;
  diffPreview?: string;
  outputLines:  string[];
  startedAt:    number;
  endedAt?:     number;
}

export interface ChatMsg {
  id:        string;
  role:      'user' | 'assistant';
  text:      string;
  thinking?: string;   // assistant-only
  agentId?:  string;
}

export interface SubAgent {
  taskId:   string;
  parent?:  string;
  status:   'running' | 'done' | 'error';
  buffer:   string;     // streaming output
  summary?: string;
}

export interface ApprovalReq {
  id:        number;
  toolName:  string;
  argsBrief: string;
}

export interface Status {
  model?:          string;
  workspace?:      string;
  gitBranch?:      string;
  gitDirty?:       boolean;
  totalTurns?:     number;
  totalToolCalls?: number;
}

export interface AppState {
  ready:        boolean;
  banner?:      {model: string; workspace: string; restored: number};
  chat:         ChatMsg[];
  liveAssistant: ChatMsg | null;   // streaming-in-progress message
  tools:        ToolCallState[];   // keyed by seq for the orchestrator
  subagents:    Record<string, SubAgent>;
  pendingApproval: ApprovalReq | null;
  status:       Status;
  errors:       string[];
  logs:         {level: string; msg: string}[];
  turnInFlight: boolean;
}

export const initialState: AppState = {
  ready: false,
  chat: [],
  liveAssistant: null,
  tools: [],
  subagents: {},
  pendingApproval: null,
  status: {},
  errors: [],
  logs: [],
  turnInFlight: false,
};

export type Action =
  | {type: 'IPC_EVENT'; evt: IpcEvent}
  | {type: 'SUBMIT_USER'; text: string}
  | {type: 'CLEAR_APPROVAL'};

const MAX_LOGS = 50;
const MAX_OUTPUT_LINES = 40;     // per tool ÃÃÃÂ¢ keep recent output, drop older
const MAX_SUBAGENT_BUF = 4000;   // chars per sub-agent stream

function genId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

export function reducer(state: AppState, action: Action): AppState {
  if (action.type === 'SUBMIT_USER') {
    const msg: ChatMsg = {id: genId(), role: 'user', text: action.text};
    return {...state, chat: [...state.chat, msg], turnInFlight: true};
  }
  if (action.type === 'CLEAR_APPROVAL') {
    return {...state, pendingApproval: null};
  }

  // IPC_EVENT
  const {evt} = action;
  const d = evt.data ?? {};

  switch (evt.type) {
    case 'ready':
      return {
        ...state,
        ready: true,
        banner: {
          model: String(d.model ?? ''),
          workspace: String(d.workspace ?? ''),
          restored: Number(d.restored_messages ?? 0),
        },
      };

    case 'status':
      return {
        ...state,
        status: {
          model:          d.model,
          workspace:      d.workspace,
          gitBranch:      d.git_branch,
          gitDirty:       d.git_dirty,
          totalTurns:     d.total_turns,
          totalToolCalls: d.total_tool_calls,
        },
      };

    case 'stream.token': {
      const tok = String(d.token ?? '');
      const agentId = d.agent_id;
      if (agentId && agentId !== 'orchestrator') {
        // Route to sub-agent buffer
        const sa = state.subagents[agentId] ?? {
          taskId: agentId, status: 'running' as const, buffer: '',
        };
        const newBuf = (sa.buffer + tok).slice(-MAX_SUBAGENT_BUF);
        return {
          ...state,
          subagents: {...state.subagents, [agentId]: {...sa, buffer: newBuf}},
        };
      }
      const live = state.liveAssistant ?? {
        id: genId(), role: 'assistant' as const, text: '', agentId: 'orchestrator',
      };
      return {...state, liveAssistant: {...live, text: live.text + tok}};
    }

    case 'stream.thinking': {
      const tok = String(d.token ?? '');
      const live = state.liveAssistant ?? {
        id: genId(), role: 'assistant' as const, text: '', agentId: 'orchestrator',
      };
      return {...state, liveAssistant: {...live, thinking: (live.thinking ?? '') + tok}};
    }

    case 'tool.start': {
      const tool: ToolCallState = {
        seq:        Number(d.seq ?? 0),
        agentId:    String(d.agent_id ?? 'orchestrator'),
        summary:    String(d.summary ?? ''),
        parallel:   Boolean(d.parallel),
        status:     'running',
        outputLines: [],
        startedAt:  evt.ts ?? Date.now() / 1000,
      };
      return {...state, tools: [...state.tools, tool]};
    }

    case 'tool.output': {
      const seq = Number(d.seq ?? 0);
      const line = String(d.line ?? '');
      return {
        ...state,
        tools: state.tools.map((t) =>
          t.seq === seq
            ? {...t, outputLines: [...t.outputLines, line].slice(-MAX_OUTPUT_LINES)}
            : t,
        ),
      };
    }

    case 'tool.end': {
      const seq = Number(d.seq ?? 0);
      return {
        ...state,
        tools: state.tools.map((t) =>
          t.seq === seq
            ? {
                ...t,
                status: d.ok ? 'ok' : 'fail',
                detail: String(d.detail ?? ''),
                diffPreview: d.diff_preview ? String(d.diff_preview) : undefined,
                endedAt: evt.ts ?? Date.now() / 1000,
              }
            : t,
        ),
      };
    }

    case 'subagent.spawn': {
      const tid = String(d.task_id ?? '');
      if (!tid) return state;
      return {
        ...state,
        subagents: {
          ...state.subagents,
          [tid]: {
            taskId: tid,
            parent: d.parent ? String(d.parent) : undefined,
            status: 'running',
            buffer: '',
            summary: d.summary ? String(d.summary) : undefined,
          },
        },
      };
    }

    case 'subagent.done': {
      const tid = String(d.task_id ?? '');
      const cur = state.subagents[tid];
      if (!cur) return state;
      return {
        ...state,
        subagents: {
          ...state.subagents,
          [tid]: {...cur, status: d.ok ? 'done' : 'error'},
        },
      };
    }

    case 'turn.done': {
      // Flush live assistant message into chat history.
      const next = {...state, turnInFlight: false, liveAssistant: null};
      if (state.liveAssistant && (state.liveAssistant.text || state.liveAssistant.thinking)) {
        next.chat = [...state.chat, state.liveAssistant];
      }
      return next;
    }

    case 'approve.request': {
      return {
        ...state,
        pendingApproval: {
          id:        Number(d.id ?? 0),
          toolName:  String(d.tool_name ?? ''),
          argsBrief: String(d.args_brief ?? ''),
        },
      };
    }

    case 'error': {
      return {...state, errors: [...state.errors, String(d.msg ?? '')]};
    }

    case 'log': {
      const log = {level: String(d.level ?? 'info'), msg: String(d.msg ?? '')};
      return {...state, logs: [...state.logs, log].slice(-MAX_LOGS)};
    }

    default:
      return state;
  }
}
