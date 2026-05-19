#!/usr/bin/env node\nimport { createRequire } from 'module'; const require = createRequire(import.meta.url);

// src/cli.tsx
import { render } from "ink";
import path from "node:path";
import { fileURLToPath } from "node:url";

// src/ipc.ts
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
var EVT = {
  READY: "ready",
  STREAM_TOKEN: "stream.token",
  STREAM_THINK: "stream.thinking",
  TOOL_START: "tool.start",
  TOOL_END: "tool.end",
  TOOL_OUTPUT: "tool.output",
  SUBAGENT_SPAWN: "subagent.spawn",
  SUBAGENT_TOKEN: "subagent.token",
  SUBAGENT_DONE: "subagent.done",
  TURN_DONE: "turn.done",
  APPROVE_REQ: "approve.request",
  ERROR: "error",
  STATUS: "status",
  LOG: "log"
};
var CMD = {
  USER_MESSAGE: "user.message",
  USER_CANCEL: "user.cancel",
  USER_APPROVE: "user.approve",
  USER_COMMAND: "user.command",
  USER_QUIT: "user.quit"
};
var IpcClient = class {
  child;
  handlers = /* @__PURE__ */ new Set();
  closed = false;
  constructor(command, opts = {}) {
    if (command.length === 0) throw new Error("IpcClient: empty command");
    const [bin, ...args] = command;
    this.child = spawn(bin, args, {
      cwd: opts.cwd,
      env: { ...process.env, ...opts.env ?? {} },
      stdio: ["pipe", "pipe", "pipe"]
    });
    const rl = createInterface({ input: this.child.stdout });
    rl.on("line", (line) => this.handleLine(line));
    const errRl = createInterface({ input: this.child.stderr });
    errRl.on("line", (line) => {
      if (!line) return;
      this.dispatch({ type: EVT.LOG, data: { level: "stderr", msg: line }, ts: Date.now() / 1e3 });
    });
    this.child.on("exit", (code, signal) => {
      this.closed = true;
      this.dispatch({
        type: EVT.LOG,
        data: { level: "info", msg: `backend exited (code=${code ?? signal})` },
        ts: Date.now() / 1e3
      });
    });
    this.child.on("error", (err) => {
      this.dispatch({ type: EVT.ERROR, data: { msg: `spawn failed: ${err.message}` }, ts: Date.now() / 1e3 });
    });
  }
  handleLine(line) {
    if (!line.trim()) return;
    let evt;
    try {
      evt = JSON.parse(line);
    } catch (e) {
      this.dispatch({
        type: EVT.LOG,
        data: { level: "warn", msg: `bad JSON from backend: ${line.slice(0, 200)}` },
        ts: Date.now() / 1e3
      });
      return;
    }
    if (typeof evt.type !== "string") return;
    if (typeof evt.data !== "object" || evt.data == null) evt.data = {};
    this.dispatch(evt);
  }
  dispatch(evt) {
    for (const h of this.handlers) {
      try {
        h(evt);
      } catch {
      }
    }
  }
  /** Subscribe.  Returns an unsubscribe callback. */
  onEvent(h) {
    this.handlers.add(h);
    return () => {
      this.handlers.delete(h);
    };
  }
  /** Send a command to the backend.  Best-effort; silently drops if closed. */
  send(type, data = {}) {
    if (this.closed) return;
    const line = JSON.stringify({ type, data }) + "\n";
    try {
      this.child.stdin.write(line);
    } catch {
      this.closed = true;
    }
  }
  /** Tell the backend to quit, then kill if it lingers. */
  shutdown(timeoutMs = 2e3) {
    if (this.closed) return;
    this.send(CMD.USER_QUIT);
    try {
      this.child.stdin.end();
    } catch {
    }
    const t = setTimeout(() => {
      try {
        this.child.kill("SIGTERM");
      } catch {
      }
    }, timeoutMs);
    this.child.once("exit", () => clearTimeout(t));
  }
};

// src/components/App.tsx
import { useEffect, useReducer, useState as useState2 } from "react";
import { Box as Box8, Text as Text8, useApp, useInput as useInput3, useStdin as useStdin2 } from "ink";

// src/state.ts
var initialState = {
  ready: false,
  chat: [],
  liveAssistant: null,
  tools: [],
  subagents: {},
  pendingApproval: null,
  status: {},
  errors: [],
  logs: [],
  turnInFlight: false
};
var MAX_LOGS = 50;
var MAX_OUTPUT_LINES = 40;
var MAX_SUBAGENT_BUF = 4e3;
function genId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}
function reducer(state, action) {
  if (action.type === "SUBMIT_USER") {
    const msg = { id: genId(), role: "user", text: action.text };
    return { ...state, chat: [...state.chat, msg], turnInFlight: true };
  }
  if (action.type === "CLEAR_APPROVAL") {
    return { ...state, pendingApproval: null };
  }
  const { evt } = action;
  const d = evt.data ?? {};
  switch (evt.type) {
    case "ready":
      return {
        ...state,
        ready: true,
        banner: {
          model: String(d.model ?? ""),
          workspace: String(d.workspace ?? ""),
          restored: Number(d.restored_messages ?? 0)
        }
      };
    case "status":
      return {
        ...state,
        status: {
          model: d.model,
          workspace: d.workspace,
          gitBranch: d.git_branch,
          gitDirty: d.git_dirty,
          totalTurns: d.total_turns,
          totalToolCalls: d.total_tool_calls
        }
      };
    case "stream.token": {
      const tok = String(d.token ?? "");
      const agentId = d.agent_id;
      if (agentId && agentId !== "orchestrator") {
        const sa = state.subagents[agentId] ?? {
          taskId: agentId,
          status: "running",
          buffer: ""
        };
        const newBuf = (sa.buffer + tok).slice(-MAX_SUBAGENT_BUF);
        return {
          ...state,
          subagents: { ...state.subagents, [agentId]: { ...sa, buffer: newBuf } }
        };
      }
      const live = state.liveAssistant ?? {
        id: genId(),
        role: "assistant",
        text: "",
        agentId: "orchestrator"
      };
      return { ...state, liveAssistant: { ...live, text: live.text + tok } };
    }
    case "stream.thinking": {
      const tok = String(d.token ?? "");
      const live = state.liveAssistant ?? {
        id: genId(),
        role: "assistant",
        text: "",
        agentId: "orchestrator"
      };
      return { ...state, liveAssistant: { ...live, thinking: (live.thinking ?? "") + tok } };
    }
    case "tool.start": {
      const tool = {
        seq: Number(d.seq ?? 0),
        agentId: String(d.agent_id ?? "orchestrator"),
        summary: String(d.summary ?? ""),
        parallel: Boolean(d.parallel),
        status: "running",
        outputLines: [],
        startedAt: evt.ts ?? Date.now() / 1e3
      };
      return { ...state, tools: [...state.tools, tool] };
    }
    case "tool.output": {
      const seq = Number(d.seq ?? 0);
      const line = String(d.line ?? "");
      return {
        ...state,
        tools: state.tools.map(
          (t) => t.seq === seq ? { ...t, outputLines: [...t.outputLines, line].slice(-MAX_OUTPUT_LINES) } : t
        )
      };
    }
    case "tool.end": {
      const seq = Number(d.seq ?? 0);
      return {
        ...state,
        tools: state.tools.map(
          (t) => t.seq === seq ? {
            ...t,
            status: d.ok ? "ok" : "fail",
            detail: String(d.detail ?? ""),
            diffPreview: d.diff_preview ? String(d.diff_preview) : void 0,
            endedAt: evt.ts ?? Date.now() / 1e3
          } : t
        )
      };
    }
    case "subagent.spawn": {
      const tid = String(d.task_id ?? "");
      if (!tid) return state;
      return {
        ...state,
        subagents: {
          ...state.subagents,
          [tid]: {
            taskId: tid,
            parent: d.parent ? String(d.parent) : void 0,
            status: "running",
            buffer: "",
            summary: d.summary ? String(d.summary) : void 0
          }
        }
      };
    }
    case "subagent.done": {
      const tid = String(d.task_id ?? "");
      const cur = state.subagents[tid];
      if (!cur) return state;
      return {
        ...state,
        subagents: {
          ...state.subagents,
          [tid]: { ...cur, status: d.ok ? "done" : "error" }
        }
      };
    }
    case "turn.done": {
      const next = { ...state, turnInFlight: false, liveAssistant: null };
      if (state.liveAssistant && (state.liveAssistant.text || state.liveAssistant.thinking)) {
        next.chat = [...state.chat, state.liveAssistant];
      }
      return next;
    }
    case "approve.request": {
      return {
        ...state,
        pendingApproval: {
          id: Number(d.id ?? 0),
          toolName: String(d.tool_name ?? ""),
          argsBrief: String(d.args_brief ?? "")
        }
      };
    }
    case "error": {
      return { ...state, errors: [...state.errors, String(d.msg ?? "")] };
    }
    case "log": {
      const log = { level: String(d.level ?? "info"), msg: String(d.msg ?? "") };
      return { ...state, logs: [...state.logs, log].slice(-MAX_LOGS) };
    }
    default:
      return state;
  }
}

// src/themes.ts
var THEMES = {
  dawn: {
    name: "Dawn",
    bg: "#faf8f5",
    surface: "#f0ede8",
    border: "#d4cfc8",
    accent: "#b8956a",
    text: "#3d3a35",
    dim: "#8a857d",
    green: "#5a8a4a",
    yellow: "#b89540",
    red: "#c06050",
    thinking: "#b0aaa0",
    pulse: "#f0c060",
    purple: "#a080c0"
  },
  sepia: {
    name: "Sepia",
    bg: "#f4f0e6",
    surface: "#e8e0d0",
    border: "#c8b898",
    accent: "#b8893a",
    text: "#4a3f30",
    dim: "#8a7a60",
    green: "#6a8a4a",
    yellow: "#c0a040",
    red: "#b85840",
    thinking: "#b0a080",
    pulse: "#e0b040",
    purple: "#9a7ab0"
  },
  ember: {
    name: "Ember",
    bg: "#1e1814",
    surface: "#2a221c",
    border: "#3a3028",
    accent: "#d4985a",
    text: "#d0c8be",
    dim: "#7a7064",
    green: "#7ab860",
    yellow: "#d4a040",
    red: "#d47050",
    thinking: "#5a5040",
    pulse: "#e89840",
    purple: "#c090d0"
  },
  slate: {
    name: "Slate",
    bg: "#111111",
    surface: "#1b1b1b",
    border: "#2a2a2a",
    accent: "#8f8f8f",
    text: "#b8b8b8",
    dim: "#5a5a5a",
    green: "#4f9f6f",
    yellow: "#b89a4a",
    red: "#a85a5a",
    thinking: "#3a3a3a",
    pulse: "#c0c040",
    purple: "#8a7ab0"
  },
  midnight: {
    name: "Midnight",
    bg: "#090b0d",
    surface: "#131619",
    border: "#1e2226",
    accent: "#8899aa",
    text: "#b0c0d0",
    dim: "#4a5560",
    green: "#4a8a6a",
    yellow: "#9a8a4a",
    red: "#9a6060",
    thinking: "#2a3040",
    pulse: "#6a8acc",
    purple: "#7a8ab0"
  },
  cobalt: {
    name: "Cobalt",
    bg: "#0a1220",
    surface: "#101830",
    border: "#1e2850",
    accent: "#6090d0",
    text: "#a0b8d8",
    dim: "#4a6090",
    green: "#5a9a6a",
    yellow: "#a0a040",
    red: "#b06060",
    thinking: "#203050",
    pulse: "#5090e0",
    purple: "#8090d0"
  },
  neon: {
    name: "Neon",
    bg: "#0c0c0c",
    surface: "#16161a",
    border: "#303030",
    accent: "#e040e0",
    text: "#c0e0c0",
    dim: "#506050",
    green: "#00e060",
    yellow: "#e0c000",
    red: "#ff4060",
    thinking: "#302040",
    pulse: "#e040ff",
    purple: "#c040ff"
  },
  forest: {
    name: "Forest",
    bg: "#0e1410",
    surface: "#141c16",
    border: "#1e2e22",
    accent: "#60a870",
    text: "#a0c0a8",
    dim: "#4a6a50",
    green: "#60d070",
    yellow: "#b0b040",
    red: "#c06050",
    thinking: "#203028",
    pulse: "#50d060",
    purple: "#8090b0"
  },
  dracula: {
    name: "Dracula",
    bg: "#282a36",
    surface: "#1e1f29",
    border: "#44475a",
    accent: "#bd93f9",
    text: "#f8f8f2",
    dim: "#6272a4",
    green: "#50fa7b",
    yellow: "#f1fa8c",
    red: "#ff5555",
    thinking: "#44475a",
    pulse: "#ff79c6",
    purple: "#bd93f9"
  }
};
var DEFAULT_THEME = "slate";
function resolveTheme(name) {
  if (!name) return THEMES[DEFAULT_THEME];
  const t = THEMES[name.toLowerCase()];
  return t ?? THEMES[DEFAULT_THEME];
}
var THEME_NAMES = Object.keys(THEMES);

// src/components/ChatPane.tsx
import { Box, Text } from "ink";
import { jsx, jsxs } from "react/jsx-runtime";
function tail(s, n) {
  if (!s) return s;
  const lines = s.split("\n");
  if (lines.length <= n) return s;
  return lines.slice(-n).join("\n");
}
var MessageView = ({ msg, theme, truncate }) => {
  const thinking = truncate ? tail(msg.thinking ?? "", 6) : msg.thinking;
  const text = truncate ? tail(msg.text, 8) : msg.text;
  if (msg.role === "user") {
    return /* @__PURE__ */ jsxs(Box, { flexDirection: "column", marginBottom: 1, children: [
      /* @__PURE__ */ jsx(Text, { color: theme.purple, bold: true, children: "you" }),
      /* @__PURE__ */ jsx(Text, { color: theme.text, children: text })
    ] });
  }
  return /* @__PURE__ */ jsxs(Box, { flexDirection: "column", marginBottom: 1, children: [
    /* @__PURE__ */ jsx(Text, { color: theme.accent, bold: true, children: "assistant" }),
    thinking && /* @__PURE__ */ jsxs(Box, { flexDirection: "column", paddingX: 1, children: [
      /* @__PURE__ */ jsx(Text, { color: theme.thinking, italic: true, children: thinking }),
      truncate && (msg.thinking?.split("\n").length ?? 0) > 6 && /* @__PURE__ */ jsx(Text, { color: theme.dim, italic: true, children: "  ... (scrolled)" })
    ] }),
    text && /* @__PURE__ */ jsx(Text, { color: theme.text, children: text }),
    truncate && msg.text.split("\n").length > 8 && /* @__PURE__ */ jsx(Text, { color: theme.dim, italic: true, children: "  ... (scrolled)" })
  ] });
};
var ChatPane = ({ chat, liveAssistant, theme, max = 30 }) => {
  const visible = chat.slice(-max);
  return /* @__PURE__ */ jsxs(Box, { flexDirection: "column", paddingX: 1, children: [
    visible.map((m) => /* @__PURE__ */ jsx(MessageView, { msg: m, theme }, m.id)),
    liveAssistant && /* @__PURE__ */ jsx(MessageView, { msg: liveAssistant, theme, truncate: true })
  ] });
};

// src/components/ToolCard.tsx
import { Box as Box3, Text as Text3 } from "ink";
import Spinner from "ink-spinner";

// src/components/CodeBlock.tsx
import { Text as Text2, Box as Box2 } from "ink";
import SyntaxHighlight from "ink-syntax-highlight";
import { jsx as jsx2, jsxs as jsxs2 } from "react/jsx-runtime";
function detectLanguage(code, hint) {
  if (hint) return hint;
  if (code.startsWith("---") || /^[-+]{1,3} /m.test(code)) return "diff";
  if (/^\s*def |^\s*class .*:/m.test(code)) return "python";
  if (/^\s*(import|export|const|function)\b/m.test(code)) return "typescript";
  return "plaintext";
}
var CodeBlock = ({ code, language, theme, maxLines }) => {
  let displayed = code;
  let truncatedCount = 0;
  if (maxLines && maxLines > 0) {
    const lines = code.split("\n");
    if (lines.length > maxLines) {
      truncatedCount = lines.length - maxLines;
      displayed = lines.slice(0, maxLines).join("\n");
    }
  }
  const lang = detectLanguage(displayed, language);
  let body;
  try {
    body = /* @__PURE__ */ jsx2(SyntaxHighlight, { code: displayed, language: lang });
  } catch {
    body = /* @__PURE__ */ jsx2(Text2, { color: theme.text, children: displayed });
  }
  return /* @__PURE__ */ jsxs2(Box2, { flexDirection: "column", borderStyle: "single", borderColor: theme.border, paddingX: 1, children: [
    body,
    truncatedCount > 0 && /* @__PURE__ */ jsxs2(Text2, { color: theme.dim, italic: true, children: [
      "... ",
      truncatedCount,
      " more line",
      truncatedCount === 1 ? "" : "s"
    ] })
  ] });
};

// src/components/ToolCard.tsx
import { jsx as jsx3, jsxs as jsxs3 } from "react/jsx-runtime";
var ToolCard = ({ tool, theme }) => {
  let glyph;
  let color = theme.yellow;
  if (tool.status === "running") {
    glyph = /* @__PURE__ */ jsx3(Spinner, { type: "dots" });
    color = theme.yellow;
  } else if (tool.status === "ok") {
    glyph = /* @__PURE__ */ jsx3(Text3, { color: theme.green, children: "OK" });
    color = theme.green;
  } else {
    glyph = /* @__PURE__ */ jsx3(Text3, { color: theme.red, children: "FAIL" });
    color = theme.red;
  }
  const tail3 = tool.outputLines.slice(-6);
  return /* @__PURE__ */ jsxs3(Box3, { flexDirection: "column", marginBottom: 1, children: [
    /* @__PURE__ */ jsxs3(Box3, { children: [
      /* @__PURE__ */ jsx3(Box3, { width: 6, children: glyph }),
      /* @__PURE__ */ jsx3(Text3, { color, bold: true, children: tool.summary }),
      tool.parallel && /* @__PURE__ */ jsx3(Text3, { color: theme.dim, children: " (parallel)" })
    ] }),
    tool.status !== "running" && tool.detail && /* @__PURE__ */ jsx3(Box3, { paddingLeft: 6, children: /* @__PURE__ */ jsx3(Text3, { color: theme.dim, children: tool.detail }) }),
    tail3.length > 0 && /* @__PURE__ */ jsx3(Box3, { flexDirection: "column", paddingLeft: 6, children: tail3.map((line, i) => /* @__PURE__ */ jsx3(Text3, { color: theme.dim, wrap: "truncate-end", children: line }, i)) }),
    tool.diffPreview && /* @__PURE__ */ jsx3(Box3, { paddingLeft: 6, marginTop: 0, children: /* @__PURE__ */ jsx3(CodeBlock, { code: tool.diffPreview, language: "diff", theme, maxLines: 20 }) })
  ] });
};

// src/components/SubAgentPanes.tsx
import { Box as Box4, Text as Text4 } from "ink";
import Spinner2 from "ink-spinner";
import { jsx as jsx4, jsxs as jsxs4 } from "react/jsx-runtime";
var TAIL_LINES = 6;
function tail2(text, n) {
  const lines = text.split("\n");
  return lines.slice(-n);
}
var SubAgentPane = ({ a, theme }) => {
  const lines = tail2(a.buffer, TAIL_LINES);
  let badge;
  let color = theme.yellow;
  if (a.status === "running") {
    badge = /* @__PURE__ */ jsx4(Spinner2, { type: "dots" });
    color = theme.yellow;
  } else if (a.status === "done") {
    badge = /* @__PURE__ */ jsx4(Text4, { color: theme.green, children: "OK" });
    color = theme.green;
  } else {
    badge = /* @__PURE__ */ jsx4(Text4, { color: theme.red, children: "X" });
    color = theme.red;
  }
  return /* @__PURE__ */ jsxs4(
    Box4,
    {
      flexDirection: "column",
      borderStyle: "single",
      borderColor: theme.border,
      paddingX: 1,
      marginRight: 1,
      width: 30,
      children: [
        /* @__PURE__ */ jsxs4(Box4, { children: [
          /* @__PURE__ */ jsx4(Box4, { width: 4, children: badge }),
          /* @__PURE__ */ jsx4(Text4, { color, bold: true, wrap: "truncate-end", children: a.taskId })
        ] }),
        a.summary && /* @__PURE__ */ jsx4(Text4, { color: theme.dim, wrap: "truncate-end", children: a.summary }),
        /* @__PURE__ */ jsx4(Box4, { flexDirection: "column", marginTop: 1, children: lines.map((l, i) => /* @__PURE__ */ jsx4(Text4, { color: theme.text, wrap: "truncate-end", children: l }, i)) })
      ]
    }
  );
};
var SubAgentPanes = ({ agents, theme }) => {
  const arr = Object.values(agents);
  if (arr.length === 0) return null;
  return /* @__PURE__ */ jsxs4(Box4, { children: [
    arr.slice(0, 4).map((a) => /* @__PURE__ */ jsx4(SubAgentPane, { a, theme }, a.taskId)),
    arr.length > 4 && /* @__PURE__ */ jsx4(Box4, { paddingX: 1, children: /* @__PURE__ */ jsxs4(Text4, { color: theme.dim, children: [
      "+",
      arr.length - 4,
      " more"
    ] }) })
  ] });
};
var AgentTree = ({ agents, theme }) => {
  const arr = Object.values(agents);
  if (arr.length === 0) {
    return /* @__PURE__ */ jsxs4(Box4, { flexDirection: "column", borderStyle: "round", borderColor: theme.border, paddingX: 1, children: [
      /* @__PURE__ */ jsx4(Text4, { color: theme.accent, bold: true, children: "agents" }),
      /* @__PURE__ */ jsx4(Text4, { color: theme.dim, italic: true, children: "orchestrator" })
    ] });
  }
  const byParent = /* @__PURE__ */ new Map();
  for (const a of arr) {
    const key = a.parent ?? "__root__";
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(a);
  }
  const renderNode = (a, depth) => {
    let glyph = theme.yellow;
    if (a.status === "done") glyph = theme.green;
    if (a.status === "error") glyph = theme.red;
    const kids = byParent.get(a.taskId) ?? [];
    return /* @__PURE__ */ jsxs4(Box4, { flexDirection: "column", children: [
      /* @__PURE__ */ jsxs4(Text4, { children: [
        /* @__PURE__ */ jsxs4(Text4, { color: theme.dim, children: [
          "  ".repeat(depth),
          "- "
        ] }),
        /* @__PURE__ */ jsxs4(Text4, { color: glyph, children: [
          a.status === "running" ? "*" : a.status === "done" ? "v" : "x",
          " "
        ] }),
        /* @__PURE__ */ jsx4(Text4, { color: theme.text, children: a.taskId })
      ] }),
      kids.map((k) => renderNode(k, depth + 1))
    ] }, a.taskId);
  };
  const roots = byParent.get("__root__") ?? [];
  return /* @__PURE__ */ jsxs4(Box4, { flexDirection: "column", borderStyle: "round", borderColor: theme.border, paddingX: 1, children: [
    /* @__PURE__ */ jsx4(Text4, { color: theme.accent, bold: true, children: "agents" }),
    /* @__PURE__ */ jsx4(Text4, { color: theme.dim, children: "orchestrator" }),
    roots.map((r) => renderNode(r, 1))
  ] });
};

// src/components/StatusBar.tsx
import { Box as Box5, Text as Text5 } from "ink";
import Spinner3 from "ink-spinner";
import { Fragment, jsx as jsx5, jsxs as jsxs5 } from "react/jsx-runtime";
function shortPath(p, n = 40) {
  if (!p) return "";
  if (p.length <= n) return p;
  return "..." + p.slice(p.length - n + 3);
}
var StatusBar = ({ status, theme, turnInFlight, themeName }) => {
  const gitLabel = status.gitBranch ? `${status.gitBranch}${status.gitDirty ? "*" : ""}` : "";
  return /* @__PURE__ */ jsxs5(Box5, { paddingX: 1, children: [
    /* @__PURE__ */ jsx5(Box5, { width: 3, children: turnInFlight ? /* @__PURE__ */ jsx5(Spinner3, { type: "dots" }) : /* @__PURE__ */ jsx5(Text5, { color: theme.dim, backgroundColor: theme.surface, children: ">>" }) }),
    /* @__PURE__ */ jsx5(Text5, { backgroundColor: theme.surface, color: theme.accent, bold: true, children: status.model ?? "" }),
    /* @__PURE__ */ jsx5(Text5, { backgroundColor: theme.surface, color: theme.dim, children: "  " }),
    /* @__PURE__ */ jsx5(Text5, { backgroundColor: theme.surface, color: theme.dim, children: shortPath(status.workspace) }),
    gitLabel && /* @__PURE__ */ jsxs5(Fragment, { children: [
      /* @__PURE__ */ jsx5(Text5, { backgroundColor: theme.surface, color: theme.dim, children: "  " }),
      /* @__PURE__ */ jsx5(Text5, { backgroundColor: theme.surface, color: theme.green, children: gitLabel })
    ] }),
    /* @__PURE__ */ jsxs5(Text5, { backgroundColor: theme.surface, color: theme.dim, children: [
      "  turns:",
      status.totalTurns ?? 0,
      " tools:",
      status.totalToolCalls ?? 0,
      "  theme:",
      themeName
    ] })
  ] });
};

// src/components/Input.tsx
import { useState, useRef } from "react";
import { Box as Box6, Text as Text6, useInput, useStdin } from "ink";
import TextInput from "ink-text-input";
import { jsx as jsx6, jsxs as jsxs6 } from "react/jsx-runtime";
var Input = ({ theme, disabled, onSubmit, onCancel, prompt = ">" }) => {
  const [value, setValue] = useState("");
  const history = useRef([]);
  const cursor = useRef(-1);
  const { isRawModeSupported } = useStdin();
  useInput((char, key) => {
    if (disabled) return;
    if (key.ctrl && char === "c") {
      onCancel?.();
      return;
    }
    if (key.upArrow && value === "" && history.current.length > 0) {
      const idx = cursor.current === -1 ? history.current.length - 1 : Math.max(0, cursor.current - 1);
      cursor.current = idx;
      setValue(history.current[idx] ?? "");
    }
    if (key.downArrow && cursor.current !== -1) {
      const idx = cursor.current + 1;
      if (idx >= history.current.length) {
        cursor.current = -1;
        setValue("");
      } else {
        cursor.current = idx;
        setValue(history.current[idx] ?? "");
      }
    }
  }, { isActive: isRawModeSupported });
  const submit = (text) => {
    if (disabled) return;
    const trimmed = text.trim();
    if (!trimmed) return;
    history.current.push(trimmed);
    cursor.current = -1;
    setValue("");
    onSubmit(trimmed);
  };
  if (!isRawModeSupported) {
    return /* @__PURE__ */ jsx6(Box6, { children: /* @__PURE__ */ jsx6(Text6, { color: theme.dim, italic: true, children: "(non-interactive stdin - input disabled)" }) });
  }
  return /* @__PURE__ */ jsxs6(Box6, { children: [
    /* @__PURE__ */ jsxs6(Text6, { color: theme.accent, children: [
      prompt,
      " "
    ] }),
    disabled ? /* @__PURE__ */ jsx6(Text6, { color: theme.dim, italic: true, children: "(working - press Ctrl-C to cancel)" }) : /* @__PURE__ */ jsx6(TextInput, { value, onChange: setValue, onSubmit: submit })
  ] });
};

// src/components/ApproveDialog.tsx
import { Box as Box7, Text as Text7, useInput as useInput2 } from "ink";
import { jsx as jsx7, jsxs as jsxs7 } from "react/jsx-runtime";
var ApproveDialog = ({ req, theme, onAnswer }) => {
  useInput2((input, key) => {
    if (input === "y" || input === "Y") onAnswer(true);
    else if (input === "n" || input === "N" || key.escape || key.return) onAnswer(false);
  });
  return /* @__PURE__ */ jsxs7(Box7, { borderStyle: "double", borderColor: theme.pulse, paddingX: 1, flexDirection: "column", children: [
    /* @__PURE__ */ jsx7(Text7, { color: theme.pulse, bold: true, children: "approval requested" }),
    /* @__PURE__ */ jsxs7(Text7, { color: theme.text, children: [
      req.toolName,
      "(",
      req.argsBrief,
      ")"
    ] }),
    /* @__PURE__ */ jsx7(Text7, { color: theme.dim, children: "press [y] to allow, [n]/Esc/Enter to deny" })
  ] });
};

// src/components/App.tsx
import { jsx as jsx8, jsxs as jsxs8 } from "react/jsx-runtime";
var SLASH_COMMANDS = /* @__PURE__ */ new Set([
  "init",
  "clear",
  "export",
  "stats",
  "session",
  "workspace",
  "help",
  "theme",
  "quit"
]);
function parseSlash(text) {
  if (!text.startsWith("/")) return null;
  const stripped = text.slice(1).trim();
  if (!stripped) return null;
  const parts = stripped.split(/\s+/);
  const name = (parts[0] ?? "").toLowerCase();
  if (!SLASH_COMMANDS.has(name)) return null;
  const rest = parts.slice(1);
  if (name === "session") {
    return { name, args: { sub: rest[0] ?? "", name: rest[1] ?? "" } };
  }
  if (name === "workspace") {
    return { name, args: { path: rest.join(" ") } };
  }
  if (name === "theme") {
    return { name, args: { theme: rest[0] ?? "" } };
  }
  return { name, args: {} };
}
var App = ({ ipc, initialTheme }) => {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [themeName, setThemeName] = useState2(initialTheme ?? DEFAULT_THEME);
  const theme = resolveTheme(themeName);
  const { exit } = useApp();
  const { isRawModeSupported } = useStdin2();
  useEffect(() => {
    const off = ipc.onEvent((evt) => dispatch({ type: "IPC_EVENT", evt }));
    return () => {
      off();
    };
  }, [ipc]);
  useInput3((input, key) => {
    if (key.ctrl && input === "q") {
      ipc.shutdown();
      setTimeout(() => exit(), 300);
    }
  }, { isActive: isRawModeSupported });
  const handleSubmit = (text) => {
    if (text === "/quit" || text === "/exit") {
      ipc.shutdown();
      setTimeout(() => exit(), 300);
      return;
    }
    if (text === "/help" || text === "/h") {
      const help = [
        "/init - reinitialize .mini_agent.rules and .mini_agent.toml",
        "/clear - reset conversation memory",
        "/export - export conversation to markdown",
        "/stats - session statistics",
        "/session list|new <n>|switch <n>|delete <n>",
        "/workspace <path>",
        `/theme <name> - one of ${THEME_NAMES.join(", ")}`,
        "/quit, /exit - exit the CLI"
      ].join("\n");
      dispatch({ type: "IPC_EVENT", evt: { type: EVT.LOG, data: { level: "info", msg: help }, ts: Date.now() / 1e3 } });
      return;
    }
    const slash = parseSlash(text);
    if (slash && slash.name === "theme") {
      const target = String(slash.args.theme ?? "").toLowerCase();
      if (THEME_NAMES.includes(target)) {
        setThemeName(target);
      } else {
        dispatch({ type: "IPC_EVENT", evt: { type: EVT.LOG, data: { level: "warn", msg: `unknown theme: ${target}` }, ts: Date.now() / 1e3 } });
      }
      return;
    }
    if (slash) {
      ipc.send(CMD.USER_COMMAND, { name: slash.name, args: slash.args });
      return;
    }
    dispatch({ type: "SUBMIT_USER", text });
    ipc.send(CMD.USER_MESSAGE, { text });
  };
  const handleApprove = (allow) => {
    if (!state.pendingApproval) return;
    ipc.send(CMD.USER_APPROVE, { id: state.pendingApproval.id, allow });
    dispatch({ type: "CLEAR_APPROVAL" });
  };
  const handleCancel = () => {
    if (state.turnInFlight) ipc.send(CMD.USER_CANCEL);
  };
  const banner = state.banner;
  const recentLog = state.logs[state.logs.length - 1];
  const recentTools = state.tools.slice(-8);
  return /* @__PURE__ */ jsxs8(Box8, { flexDirection: "column", children: [
    /* @__PURE__ */ jsxs8(Box8, { paddingX: 1, children: [
      /* @__PURE__ */ jsx8(Text8, { backgroundColor: theme.surface, color: theme.accent, bold: true, children: "mini_agent" }),
      banner && /* @__PURE__ */ jsxs8(Text8, { backgroundColor: theme.surface, color: theme.dim, children: [
        "  ",
        banner.model,
        "  -  ",
        banner.workspace,
        banner.restored > 0 ? `  (restored ${banner.restored} msgs)` : ""
      ] })
    ] }),
    /* @__PURE__ */ jsxs8(Box8, { flexDirection: "column", paddingX: 1, children: [
      /* @__PURE__ */ jsxs8(Box8, { borderStyle: "single", borderColor: theme.border, paddingX: 1, marginBottom: 1, children: [
        /* @__PURE__ */ jsx8(Text8, { color: theme.accent, bold: true, children: "tools" }),
        recentTools.length > 0 && /* @__PURE__ */ jsxs8(Text8, { color: theme.dim, children: [
          "  (",
          recentTools.length,
          " calls)"
        ] }),
        recentTools.length === 0 && /* @__PURE__ */ jsx8(Text8, { color: theme.dim, italic: true, children: "  no tool calls yet" })
      ] }),
      recentTools.map((t) => /* @__PURE__ */ jsx8(ToolCard, { tool: t, theme }, t.seq))
    ] }),
    /* @__PURE__ */ jsx8(AgentTree, { agents: state.subagents, theme }),
    /* @__PURE__ */ jsx8(SubAgentPanes, { agents: state.subagents, theme }),
    /* @__PURE__ */ jsx8(ChatPane, { chat: state.chat, liveAssistant: state.liveAssistant, theme, max: 8 }),
    state.errors.slice(-3).map((e, i) => /* @__PURE__ */ jsx8(Box8, { paddingX: 1, children: /* @__PURE__ */ jsxs8(Text8, { color: theme.red, children: [
      "error: ",
      e
    ] }) }, i)),
    recentLog && /* @__PURE__ */ jsx8(Box8, { paddingX: 1, children: /* @__PURE__ */ jsx8(
      Text8,
      {
        color: recentLog.level === "warn" || recentLog.level === "stderr" ? theme.yellow : theme.dim,
        wrap: "wrap",
        children: recentLog.msg
      }
    ) }),
    state.pendingApproval && /* @__PURE__ */ jsx8(ApproveDialog, { req: state.pendingApproval, theme, onAnswer: handleApprove }),
    /* @__PURE__ */ jsx8(Input, { theme, disabled: state.turnInFlight, onSubmit: handleSubmit, onCancel: handleCancel }),
    /* @__PURE__ */ jsx8(StatusBar, { status: state.status, theme, turnInFlight: state.turnInFlight, themeName })
  ] });
};

// src/cli.tsx
import { jsx as jsx9 } from "react/jsx-runtime";
function parseArgs(argv) {
  const out = { pythonCmd: process.env.MINI_AGENT_PYTHON ?? "python3", backendArgs: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--python-cmd" && i + 1 < argv.length) {
      out.pythonCmd = argv[++i] ?? out.pythonCmd;
    } else if (a === "--theme" && i + 1 < argv.length) {
      out.theme = argv[++i];
    } else {
      out.backendArgs.push(a);
    }
  }
  return out;
}
function resolveBackendScript() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  return path.resolve(here, "..", "..", "mini_agent_headless.py");
}
function main() {
  const args = parseArgs(process.argv.slice(2));
  const script = process.env.MINI_AGENT_BACKEND ?? resolveBackendScript();
  const cmd = [args.pythonCmd, script, ...args.backendArgs];
  const ipc = new IpcClient(cmd, { env: { PYTHONUNBUFFERED: "1" } });
  const themeFromEnv = process.env.MINI_AGENT_THEME;
  const initialTheme = args.theme ?? themeFromEnv;
  const { waitUntilExit } = render(/* @__PURE__ */ jsx9(App, { ipc, initialTheme }));
  const shutdown = () => {
    ipc.shutdown();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
  waitUntilExit().then(() => {
    ipc.shutdown();
    process.exit(0);
  });
}
main();
