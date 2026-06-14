import { useState, useRef, useEffect, useCallback } from 'react';
import useSmoothStream from './hooks/useSmoothStream';
import LogLine from './components/LogLine';
import CodeBlock from './components/CodeBlock';
import LogPanel from './components/LogPanel';
import AgentTree from './components/AgentTree';
import RoundedFrame from './components/RoundedFrame';
import CharStream from './components/CharStream';
import DeferredMarkdown from './components/DeferredMarkdown';
import StreamingMessage from './components/StreamingMessage';
import ErrorBoundary from './components/ErrorBoundary';
import SessionPicker from './components/SessionPicker';
import SettingsPanel from './components/SettingsPanel';

// Cap rendered DOM nodes to prevent lag at long conversations (300+ turns).
// State arrays still hold full history; only the visible slice hits the DOM.
const MAX_RENDERED_CHAT_LINES = 400;
const MAX_RENDERED_TOOL_LINES = 400;

// Theme registry -- name, data-theme value, status-bar icon (colored circle + palette)
const PALETTE_SVG = <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="5" cy="8" r="2.5"/><circle cx="12" cy="4" r="2"/><circle cx="12" cy="11.5" r="2"/><path d="M3 13a3 3 0 0 0 5.2-2 1.8 1.8 0 0 1 2.1-1.8A3 3 0 0 0 13 6"/></svg>;
const THEME_COLORS = {
  dark:         '#a0a8c0',
  light:        '#e8ac4a',
  dracula:      '#bd93f9',
  nord:         '#88c0d0',
  catppuccin:   '#cba6f7',
  'rose-pine':  '#ebbcba',
  gruvbox:      '#d79921',
  solarized:    '#2aa198',
  'tokyo-night':'#7aa2f7',
  monokai:      '#a6e22e',
};
const THEMES = [
  { name: 'Dark',         id: 'dark',         icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS.dark}/></svg> },
  { name: 'Light',        id: 'light',        icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS.light}/></svg> },
  { name: 'Dracula',      id: 'dracula',      icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS.dracula}/></svg> },
  { name: 'Nord',         id: 'nord',         icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS.nord}/></svg> },
  { name: 'Catppuccin',   id: 'catppuccin',   icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS.catppuccin}/></svg> },
  { name: 'Rose Pine',    id: 'rose-pine',    icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS['rose-pine']}/></svg> },
  { name: 'Gruvbox',      id: 'gruvbox',      icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS.gruvbox}/></svg> },
  { name: 'Solarized',    id: 'solarized',    icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS.solarized}/></svg> },
  { name: 'Tokyo Night',  id: 'tokyo-night',  icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS['tokyo-night']}/></svg> },
  { name: 'Monokai',      id: 'monokai',      icon: <svg viewBox="0 0 12 12" width="10" height="10"><circle cx="6" cy="6" r="4" fill={THEME_COLORS.monokai}/></svg> },
];

// Model catalog for the clickable model picker dropdown.
//
// DIRECT_MODEL_GROUPS: models accessible via their native provider APIs.
//   Bare IDs trigger provider switch in server.py's set_model().
//   Requires corresponding API key env var (DEEPSEEK_API_KEY, MOONSHOT_API_KEY, etc.).
//
// OPENROUTER_MODEL_GROUPS: models routed through OpenRouter's unified API.
//   Prefixed IDs (provider/model) are sent to OpenRouter which handles routing.
//   Requires OPENROUTER_API_KEY env var.  Free models use the :free suffix.

const DIRECT_MODEL_GROUPS = [
  { group: 'DeepSeek', models: [
    { id: 'deepseek-v4-pro',   label: 'DeepSeek V4 Pro' },
    { id: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash' },
  ]},
  { group: 'Kimi / Moonshot', models: [
    { id: 'kimi-k2.7-code', label: 'Kimi K2.7 Code' },
    { id: 'kimi-k2.6',      label: 'Kimi K2.6' },
  ]},
  { group: 'Qwen (DashScope)', models: [
    { id: 'qwen-plus',    label: 'Qwen-Plus' },
    { id: 'qwen-flash',   label: 'Qwen-Flash' },
    { id: 'qwen3-max',    label: 'Qwen 3 Max' },
    { id: 'qwen3-coder',  label: 'Qwen 3 Coder' },
  ]},
  { group: 'Free Tier', models: [
    { id: 'gemini-3.5-flash', label: 'Gemini 3.5 Flash (free)' },
  ]},
];

const OPENROUTER_MODEL_GROUPS = [
  { group: 'Kimi / Moonshot', models: [
    { id: 'moonshotai/kimi-k2.7-code', label: 'Kimi K2.7 Code' },
    { id: 'moonshotai/kimi-k2.6',      label: 'Kimi K2.6' },
  ]},
  { group: 'Google / Gemini', models: [
    { id: 'google/gemini-3.5-flash', label: 'Gemini 3.5 Flash' },
    { id: 'google/gemini-3.5-pro',   label: 'Gemini 3.5 Pro' },
  ]},
  { group: 'Qwen (DashScope)', models: [
    { id: 'qwen/qwen-plus',    label: 'Qwen-Plus' },
    { id: 'qwen/qwen3-max',    label: 'Qwen 3 Max' },
    { id: 'qwen/qwen3-coder',  label: 'Qwen 3 Coder' },
  ]},
  { group: 'Free Models', models: [
    { id: 'deepseek/deepseek-v4-flash:free',   label: 'DeepSeek V4 Flash (free)' },
    { id: 'qwen/qwen3-coder:free',             label: 'Qwen 3 Coder (free)' },
    { id: 'google/gemma-4-31b-it:free',        label: 'Gemma 4 31B (free)' },
    { id: 'openai/gpt-oss-120b:free',          label: 'GPT-OSS 120B (free)' },
    { id: 'meta-llama/llama-3.3-70b-instruct:free', label: 'Llama 3.3 70B (free)' },
    { id: 'openrouter/free',                   label: 'OpenRouter Free Router' },
  ]},
];

function setThemeDom(id) {
  document.documentElement.setAttribute('data-theme', id);
  localStorage.setItem('mini_agent_theme', id);
}


// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
function AppShell() {
  // Log state -- arrays of { text, cls?, html?, icon? }
  const [toolsLines, setToolsLines] = useState([]);
  const [chatLines, setChatLines] = useState([]);

  // Sub-agent data -- { [task_id]: { name, desc, toolCalls: [], thoughts: [], output: "", ok: null } }
  const [subagentData, setSubagentData] = useState({});

  // Smooth streaming for thinking & chat
  const thinking = useSmoothStream();
  const chatStream = useSmoothStream();

  // UI state
  const [modelName, setModelName] = useState('starting...');
  const [sessionName, setSessionName] = useState('');
  const [gitBranch, setGitBranch] = useState('');
  const [gitDirty, setGitDirty] = useState(false);
  const [workspace, setWorkspace] = useState('');
  const [restoredCount, setRestoredCount] = useState(null);
  const [isLive, setIsLive] = useState(false);
  const [turnCountVal, setTurnCountVal] = useState(null);
  const [tokenCountVal, setTokenCountVal] = useState(null);
  const [elapsedSec, setElapsedSec] = useState(null);
  const [inputDisabled, setInputDisabled] = useState(false);
  const [thinkingBlocks, setThinkingBlocks] = useState([]);
  const [botStatus, setBotStatus] = useState({});

  const inputRef = useRef(null);
  const thinkingLogRef = useRef(null);
  const chatLogRef = useRef(null);
  const inThinkingRef = useRef(false);
  const submitTimeoutRef = useRef(null);
  const timerRef = useRef(null);
  const turnStartRef = useRef(null);
  const toolOutputStack = useRef([]); // stack of buffers for parallel tool calls
  const lineIdRef = useRef(0); // monotonically increasing ID for stable React keys
  const nextLineId = useCallback(() => ++lineIdRef.current, []);

  const startTimer = useCallback(() => {
    if (timerRef.current) return; // already running
    turnStartRef.current = Date.now();
    setElapsedSec(0);
    timerRef.current = setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - turnStartRef.current) / 1000));
    }, 1000);
  }, []);

  const stopTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (turnStartRef.current) {
      setElapsedSec(Math.floor((Date.now() - turnStartRef.current) / 1000));
      turnStartRef.current = null;
    }
  }, []);
  const [showSettings, setShowSettings] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const [theme, setTheme] = useState(() => localStorage.getItem('mini_agent_theme') || 'dark');
  const [themePickerOpen, setThemePickerOpen] = useState(false);
  const themeToggleRef = useRef(null);
  const [dropdownPos, setDropdownPos] = useState(null);

  // Model picker
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [provider, setProvider] = useState('deepseek');
  const modelRef = useRef(null);
  const [modelDropdownPos, setModelDropdownPos] = useState(null);

  const themeIndex = THEMES.findIndex((t) => t.id === theme);
  const themeEntry = THEMES[themeIndex] || THEMES[0];

  const applyTheme = useCallback((id) => {
    setTheme(id);
    setThemeDom(id);
    setThemePickerOpen(false);
  }, []);

  // Cycle to next theme
  const cycleTheme = useCallback(() => {
    const nextIndex = (themeIndex + 1) % THEMES.length;
    applyTheme(THEMES[nextIndex].id);
  }, [themeIndex, applyTheme]);

  // Close theme picker on outside click
  useEffect(() => {
    if (!themePickerOpen) return;
    const close = (e) => {
      if (!e.target.closest('.theme-dropdown') && !e.target.closest('#theme-toggle')) {
        setThemePickerOpen(false);
      }
    };
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, [themePickerOpen]);

  // Position the theme dropdown relative to the toggle icon
  useEffect(() => {
    if (!themePickerOpen || !themeToggleRef.current) {
      setDropdownPos(null);
      return;
    }
    const rect = themeToggleRef.current.getBoundingClientRect();
    const dropdownW = 190;
    let right = window.innerWidth - rect.right;
    // Clamp so dropdown doesn't overflow right edge
    if (right + dropdownW > window.innerWidth - 8) {
      right = Math.max(4, window.innerWidth - dropdownW - 8);
    }
    setDropdownPos({
      bottom: window.innerHeight - rect.top + 4,
      right,
    });
  }, [themePickerOpen]);

  // Position the model dropdown relative to the header model span
  useEffect(() => {
    if (!modelPickerOpen || !modelRef.current) {
      setModelDropdownPos(null);
      return;
    }
    const rect = modelRef.current.getBoundingClientRect();
    const dropdownW = 240;
    let left = rect.left;
    if (left + dropdownW > window.innerWidth - 8) {
      left = Math.max(4, window.innerWidth - dropdownW - 8);
    }
    setModelDropdownPos({
      top: rect.bottom + 4,
      left,
    });
  }, [modelPickerOpen]);

  // Close model picker on outside click
  useEffect(() => {
    if (!modelPickerOpen) return;
    const close = (e) => {
      if (!e.target.closest('.model-dropdown') && !e.target.closest('#header-model')) {
        setModelPickerOpen(false);
      }
    };
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, [modelPickerOpen]);

  // Helper to add a line to any log
  const addLine = useCallback((setter) => (line) => {
    setter((prev) => [...prev, line]);
  }, []);

  const addToolLine = useCallback((line) => addLine(setToolsLines)(line), [addLine]);

  // Status / init -- fetched once on mount (empty deps to avoid re-render loop)
  useEffect(() => {
    const api = window.miniAgent;
    if (!api) return;

    const onStatus = (data) => {
      // Check for no-API-key signal from main process
      if (data.reason === 'no_api_key') {
        setShowSettings(true);
        return;
      }
      if (data.ready) {
        // Backend came online -- hide settings if it was showing
        setShowSettings(false);
      }
      if (data.model != null) setModelName(data.model);
      if (data.provider != null) setProvider(data.provider);
      if (data.session_name != null) setSessionName(data.session_name);
      if (data.workspace != null) setWorkspace(data.workspace);
      if (data.git_branch != null) {
        setGitBranch(data.git_branch);
        setGitDirty(!!data.git_dirty);
      }
      if (data.restored_count != null) setRestoredCount(data.restored_count);
      if (data.ready) {
        addToolLine({ text: 'backend ready', cls: 'dim' });
      }
    };
    const unsub = api.on('backend:status', onStatus);

    // Fetch cached status from main process (handles race where backend
    // sent status before our listener was registered)
    api.getStatus?.().then((data) => {
      if (!data) return;
      onStatus(data);
    });

    return () => unsub();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Discord bot status listener
  useEffect(() => {
    const api = window.miniAgent;
    if (!api) return;
    const unsub = api.on('backend:bot_status', (data) => {
      setBotStatus((prev) => ({ ...prev, [data.name]: data.alive }));
    });
    return () => unsub();
  }, []);

  // Stream listeners
  useEffect(() => {
    const api = window.miniAgent;
    if (!api) return;

    const unsubs = [];

    unsubs.push(api.on('stream:token', (data) => {
      if (inThinkingRef.current) {
        thinking.addChunk(data.text);
      } else {
        chatStream.addChunk(data.text);
      }
    }));

    unsubs.push(api.on('stream:thinking_start', () => {
      inThinkingRef.current = true;
      thinking.reset();
    }));

    unsubs.push(api.on('stream:thinking_end', () => {
      inThinkingRef.current = false;
      const flushed = thinking.flush();
      if (flushed) setThinkingBlocks((prev) => [...prev, flushed]);
    }));

    unsubs.push(api.on('stream:tool_start', (data) => {
      addToolLine({ text: '', cls: 'tool-separator' });
      // Color-code the tool name using structured data (not HTML strings)
      const summary = data.summary;
      const parenIdx = summary.indexOf('(');
      let toolName, toolArgs;
      if (parenIdx > 0) {
        toolName = summary.slice(0, parenIdx);
        toolArgs = summary.slice(parenIdx);
      } else {
        toolName = summary;
        toolArgs = '';
      }
      addToolLine({
        toolName,
        toolArgs,
        cls: '',
      });
      // Push a new buffer for this tool call (stack supports parallel calls)
      // Track tool name alongside the buffer for language detection
      toolOutputStack.current.push({ lines: [], toolName: data.summary });
    }));

    unsubs.push(api.on('stream:tool_output', (data) => {
      const lines = data.line.split('\n');
      const entry = toolOutputStack.current[toolOutputStack.current.length - 1];
      if (entry) {
        for (const line of lines) {
          entry.lines.push(line);
        }
      }
    }));

    unsubs.push(api.on('stream:tool_end', (data) => {
      const status = data.ok ? 'OK' : 'ERR';
      const cls = data.ok ? 'msg-tool-ok' : 'msg-tool-err';
      // Pop this tool's buffer from the stack (supports parallel calls)
      const entry = toolOutputStack.current.pop() || { lines: [], toolName: '' };
      const bufCode = entry.lines.join('\n').trim();
      const code = bufCode || (data.content || '').trim();
      // When output is present, show it with syntax highlighting
      if (code) {
        const isSingleLine = !code.includes('\n');
        if (isSingleLine) {
          addToolLine({ text: `  ${status}  ${code}`, cls });
        } else {
          addToolLine({ text: `  ${status}`, cls });
          addToolLine({
            component: <CodeBlock code={code} fontSize="0.75em" toolName={entry.toolName} />,
            cls: '',
          });
        }
      } else {
        addToolLine({ text: `  ${status} ${data.detail}`, cls });
      }
    }));

    unsubs.push(api.on('stream:turn_complete', (data) => {
      clearTimeout(submitTimeoutRef.current);
      const agentText = chatStream.flush();
      if (agentText) {
        setChatLines((prev) => {
          const updated = [...prev];
          if (updated.length > 0 && updated[updated.length - 1].cls === 'msg-agent-pending') {
            updated[updated.length - 1] = { id: updated[updated.length - 1].id, text: agentText, cls: 'msg-agent', markdown: true };
          } else {
            updated.push({ id: nextLineId(), text: agentText, cls: 'msg-agent', markdown: true });
          }
          return updated;
        });
        chatStream.reset();
      }
      if (data.usage?.total_tokens) {
        const tok = data.usage.total_tokens;
        setTokenCountVal(tok >= 1000 ? `${(tok / 1000).toFixed(1)}k` : String(tok));
      }
      if (data.turn_count) setTurnCountVal(data.turn_count);
      // NOTE: Do NOT set isLive=false here.  The agent may start another
      // turn immediately (sub-agent auto-report, tool continuations, etc.).
      // Only the 'idle' message (sent when _turn_loop truly drains the
      // queue) should reset isLive.
    }));

    unsubs.push(api.on('stream:error', (data) => {
      clearTimeout(submitTimeoutRef.current);
      stopTimer();
      chatStream.flush();
      chatStream.reset();
      setChatLines((prev) => [...prev, { id: nextLineId(), text: `Error: ${data.message}`, cls: 'msg-error' }]);
      setIsLive(false);
      setInputDisabled(false);
      inputRef.current?.focus();
    }));

    unsubs.push(api.on('stream:status', (data) => {
      setChatLines((prev) => [...prev, { id: nextLineId(), text: data.message, cls: 'msg-status' }]);
    }));

    unsubs.push(api.on('backend:response', (data) => {
      if (data.lines) {
        for (const line of data.lines) {
          setChatLines((prev) => [...prev, { id: nextLineId(), text: line, cls: 'msg-status' }]);
        }
      }
    }));

    // --- Turn lifecycle: start / idle ---
    // The backend sends turn_start at the beginning of each turn and idle
    // when the sequential turn-loop truly exits (input queue drained).
    // These provide a reliable running/cancel indicator that doesn't flicker
    // between turns.
    unsubs.push(api.on('backend:turn_start', () => {
      setIsLive(true);
      setInputDisabled(true);
      startTimer();
    }));

    unsubs.push(api.on('backend:idle', () => {
      clearTimeout(submitTimeoutRef.current);
      stopTimer();
      setIsLive(false);
      setInputDisabled(false);
      inputRef.current?.focus();
    }));

    // --- Sub-agent events ---
    unsubs.push(api.on('stream:subagent_start', (data) => {
      setSubagentData((prev) => ({
        ...prev,
        [data.task_id]: {
          name: data.name,
          desc: data.desc,
          parent_id: data.parent_id || 'orchestrator',
          toolCalls: [],
          thoughts: [],
          output: '',
          ok: null,
        },
      }));
    }));

    unsubs.push(api.on('stream:subagent_tool_start', (data) => {
      setSubagentData((prev) => {
        const agent = prev[data.task_id];
        if (!agent) return prev;
        return {
          ...prev,
          [data.task_id]: {
            ...agent,
            toolCalls: [...agent.toolCalls, {
              toolName: data.tool_name,
              toolArgs: data.tool_args ? `(${data.tool_args.slice(0, 80)})` : '',
              ok: null,
            }],
          },
        };
      });
    }));

    unsubs.push(api.on('stream:subagent_tool_end', (data) => {
      setSubagentData((prev) => {
        const agent = prev[data.task_id];
        if (!agent) return prev;
        const toolCalls = [...agent.toolCalls];
        // Mark the last matching tool call as complete
        for (let i = toolCalls.length - 1; i >= 0; i--) {
          if (toolCalls[i].toolName === data.tool_name && toolCalls[i].ok === null) {
            toolCalls[i] = { ...toolCalls[i], ok: data.ok, result: data.content?.slice(0, 200) };
            break;
          }
        }
        return { ...prev, [data.task_id]: { ...agent, toolCalls } };
      });
    }));

    unsubs.push(api.on('stream:subagent_thought', (data) => {
      setSubagentData((prev) => {
        const agent = prev[data.task_id];
        if (!agent) return prev;
        // Keep last 30 thought chunks to avoid unbounded growth
        const thoughts = [...agent.thoughts, data.text].slice(-30);
        return { ...prev, [data.task_id]: { ...agent, thoughts } };
      });
    }));

    unsubs.push(api.on('stream:subagent_output', (data) => {
      // subagent_output is still sent for backward compat; accumulate into thoughts
      setSubagentData((prev) => {
        const agent = prev[data.task_id];
        if (!agent) return prev;
        const thoughts = [...agent.thoughts, data.line].slice(-30);
        return { ...prev, [data.task_id]: { ...agent, thoughts } };
      });
    }));

    unsubs.push(api.on('stream:subagent_end', (data) => {
      setSubagentData((prev) => {
        const agent = prev[data.task_id];
        if (!agent) return prev;
        return {
          ...prev,
          [data.task_id]: {
            ...agent,
            ok: data.ok,
            output: data.content || '',
          },
        };
      });
    }));

    return () => unsubs.forEach((fn) => fn());
  }, []); // stable: addToolLine/thinking/chatStream callbacks are useCallback-wrapped

  // Submit handler
  const handleSubmit = useCallback((text) => {
    if (!text) return;

    // Allow /clear (and cancel) even during an active turn so the user
    // isn't trapped in a runaway agent loop. Reject all other input.
    if (inputDisabled) {
      if (text.trim().toLowerCase() === '/clear') {
        window.miniAgent.cancel();          // kill running turn
        setChatLines([]);
        setToolsLines([]);
        setSubagentData({});
        chatStream.reset();
        thinking.reset();
        setThinkingBlocks([]);
        setIsLive(false);
        setInputDisabled(false);
        setInputValue('');
        inputRef.current?.focus();
        window.miniAgent.command('/clear'); // tell backend to wipe memory
      }
      return;
    }

    if (text.startsWith('/')) {
      // Handle renderer-local commands first
      const trimmed = text.trim().toLowerCase();
      if (trimmed.startsWith('/theme')) {
        const arg = trimmed.replace('/theme', '').trim();
        if (arg) {
          // `/theme <name>` -- fuzzy match against theme id or name
          const match = THEMES.find((t) =>
            t.id.toLowerCase() === arg.toLowerCase() ||
            t.name.toLowerCase() === arg.toLowerCase()
          );
          if (match) {
            applyTheme(match.id);
          }
        } else {
          // `/theme` with no arg -- cycle
          cycleTheme();
        }
        setInputValue('');
        return;
      }
      window.miniAgent.command(text);
      setInputValue('');
      // /clear also wipes the renderer's chat & tool logs immediately
      if (text.trim().toLowerCase() === '/clear') {
        setChatLines([]);
        setToolsLines([]);
        setSubagentData({});
        chatStream.reset();
        thinking.reset();
        setThinkingBlocks([]);
      }
      return;
    }

    setChatLines((prev) => [
      ...prev,
      ...(prev.length > 0 ? [{ id: nextLineId(), text: '', cls: 'msg-separator' }] : []),
      { id: nextLineId(), text, cls: 'msg-user' },
      { id: nextLineId(), text: '', cls: 'msg-separator' },
      { id: nextLineId(), text: '', cls: 'msg-agent-pending' },
    ]);
    chatStream.reset();

    setIsLive(true);
    setInputDisabled(true);
    setInputValue('');

    window.miniAgent.submit(text);

    // Safety timeout -- re-enable input after 120s in case the backend
    // hangs or crashes.  The idle message handles normal completion;
    // this is a last-resort fallback.
    submitTimeoutRef.current = setTimeout(() => {
      setInputDisabled(false);
      inputRef.current?.focus();
    }, 120_000);
  }, [inputDisabled, chatStream]);

  const handleKeyDown = useCallback((e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e.target.value);
    }
  }, [handleSubmit]);

  const handleChange = useCallback((e) => {
    setInputValue(e.target.value);
  }, []);

  // Drag-and-drop: use the preload bridge which can read Electron's File.path.
  // The preload manages dragOver/drop at the document level and calls our
  // callback with absolute file paths.
  useEffect(() => {
    const api = window.miniAgent;
    if (!api || !api.onFileDrop) return;
    const unsub = api.onFileDrop((paths) => {
      setInputValue((prev) => {
        const appended = paths.join(' ');
        return prev ? `${prev} ${appended}` : appended;
      });
      inputRef.current?.focus();
    });
    return () => unsub();
  }, []);

  // Click workspace to change it
  const handleWorkspaceClick = useCallback(async () => {
    const api = window.miniAgent;
    if (!api) return;
    const newPath = await api.openWorkspace();
    if (newPath) {
      api.saveWorkspace(newPath);
      handleSubmit(`/workspace ${newPath}`);
    }
  }, [handleSubmit]);

  // Session picker handler
  const handleSessionSwitch = useCallback((name, isNew) => {
    const api = window.miniAgent;
    if (!api) return;
    if (isNew) {
      api.newSession(name);
    } else {
      api.switchSession(name);
    }
    // Session name in footer will update via backend:status event
  }, []);

  // Settings saved handler -- backend will send backend:status { ready: true }
  // which triggers setShowSettings(false) in the onStatus listener
  const handleSettingsSaved = useCallback(() => {
    // Let the backend:status event handle hiding the panel
  }, []);

  // Cancel handler -- immediately reset UI, then tell backend
  const handleCancel = useCallback(() => {
    window.miniAgent?.cancel();
    clearTimeout(submitTimeoutRef.current);
    stopTimer();
    inThinkingRef.current = false;
    const agentText = chatStream.flush();
    const thinkText = thinking.flush();
    if (thinkText) setThinkingBlocks((prev) => [...prev, thinkText]);
    thinking.reset();
    if (agentText) {
      setChatLines((prev) => {
        const updated = [...prev];
        if (updated.length > 0 && updated[updated.length - 1].cls === 'msg-agent-pending') {
          updated[updated.length - 1] = { id: updated[updated.length - 1].id, text: agentText, cls: 'msg-agent' };
        }
        return updated;
      });
      chatStream.reset();
    }
    setIsLive(false);
    setInputDisabled(false);
    setInputValue('');
    inputRef.current?.focus();
  }, [chatStream, thinking, stopTimer]);

  // Discord bot start/stop toggle
  const BOT_SCRIPTS = { 'mini-agent': 'workspace_bot.py', 'emotion-game': 'discord_bot.py' };
  const handleBotToggle = useCallback(async (botName) => {
    const api = window.miniAgent;
    if (!api) return;
    const script = BOT_SCRIPTS[botName];
    if (!script) return;
    const current = botStatus[botName];
    setBotStatus((prev) => ({ ...prev, [botName]: !current }));
    try {
      if (current) {
        await api.stopBot(script);
      } else {
        await api.startBot(script);
      }
    } catch (e) {
      setBotStatus((prev) => ({ ...prev, [botName]: current }));
    }
  }, [botStatus]);

  // Auto-scroll thinking log
  useEffect(() => {
    if (thinkingLogRef.current) {
      thinkingLogRef.current.scrollTop = thinkingLogRef.current.scrollHeight;
    }
  }, [thinking.displayedText]);

  // Auto-scroll chat log
  useEffect(() => {
    if (chatLogRef.current) {
      chatLogRef.current.scrollTop = chatLogRef.current.scrollHeight;
    }
  }, [chatLines, chatStream.displayedText]);

  // Auto-focus input on mount
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      clearTimeout(submitTimeoutRef.current);
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  return (
    <div id="app">
      {/* Header */}
      <div id="header" className="header">
        <span className="dim"> mini_agent -- </span>
        <span
          id="header-model"
          className="text clickable"
          ref={modelRef}
          onClick={() => setModelPickerOpen((p) => !p)}
          title="Click to switch model"
        >{modelName}</span>
        {modelPickerOpen && modelDropdownPos && (
          <div className="model-dropdown" style={modelDropdownPos} onClick={(e) => e.stopPropagation()}>
            {/* DIRECT API section */}
            <div className="model-dropdown-section">
              <div className="model-dropdown-header model-dropdown-section-header">── DIRECT API ──</div>
              {DIRECT_MODEL_GROUPS.map((grp, gi) => (
                <div key={`direct-${gi}`}>
                  <div className="model-dropdown-subheader">{grp.group}</div>
                  {grp.models.map((m) => {
                    const isCurrent = m.id === modelName;
                    return (
                      <div
                        key={m.id}
                        className={`model-dropdown-item${isCurrent ? ' model-current' : ''}`}
                        onClick={(e) => { e.stopPropagation(); setModelPickerOpen(false); window.miniAgent?.setModel(m.id); }}
                      >
                        <span className="model-name">{m.label}</span>
                        <span className="model-id dim">{m.id}</span>
                        {isCurrent && <span className="model-check">{'\u2713'}</span>}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>

            {/* OPENROUTER section */}
            <div className="model-dropdown-section">
              <div className="model-dropdown-header model-dropdown-section-header">── OPENROUTER ──</div>
              {OPENROUTER_MODEL_GROUPS.map((grp, gi) => (
                <div key={`or-${gi}`}>
                  <div className="model-dropdown-subheader">{grp.group}</div>
                  {grp.models.map((m) => {
                    const isCurrent = m.id === modelName;
                    return (
                      <div
                        key={m.id}
                        className={`model-dropdown-item${isCurrent ? ' model-current' : ''}`}
                        onClick={(e) => { e.stopPropagation(); setModelPickerOpen(false); window.miniAgent?.setModel(m.id); }}
                      >
                        <span className="model-name">{m.label}</span>
                        <span className="model-id dim">{m.id}</span>
                        {isCurrent && <span className="model-check">{'\u2713'}</span>}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Body: three panels */}
      <div id="body-panels">
        {/* Left stack: Tools & Thinking + Agent Tree */}
        <div id="left-stack">
          <RoundedFrame id="left-pane">
            <LogPanel id="tools-log" className="scrollable dim" lines={toolsLines.slice(-MAX_RENDERED_TOOL_LINES)} />
            <div className="hr" />
            <div id="thinking-log" ref={thinkingLogRef} className="log thinking-log thinking">
              {thinking.displayedText && (
                <CharStream text={thinking.displayedText} className="msg-thinking" />
              )}
              {!thinking.displayedText && thinkingBlocks.map((block, i) => (
                <div key={i} className="msg-thinking">
                  <DeferredMarkdown text={block} markdown={false} />
                </div>
              ))}
            </div>
          </RoundedFrame>
          {Object.keys(subagentData).length > 0 && (
            <div id="agent-tree-panel">
              <AgentTree agents={subagentData} />
            </div>
          )}
        </div>

        {/* Right pane: Chat */}
        <RoundedFrame id="right-pane">
          <div id="chat-log" ref={chatLogRef} className="log scrollable text">
            {chatLines.slice(-MAX_RENDERED_CHAT_LINES).map((line) => {
              if (line.cls === 'msg-agent') {
                return (
                  <div key={line.id} className="msg-agent">
                    <DeferredMarkdown text={line.text} />
                  </div>
                );
              }
              return <LogLine key={line.id} line={line} />;
            })}
            {chatStream.displayedText && (
              <div className="msg-agent">
                <StreamingMessage text={chatStream.displayedText} />
              </div>
            )}
          </div>
        </RoundedFrame>
      </div>

      {/* Input */}
      <div id="input-frame" className={`rounded-frame${isLive ? ' live' : ''}`}>
        <div className="frame-body">
          <div className="frame-content">
            <div id="input-container">
              <span className="prompt">{'>'}</span>
              <input
                ref={inputRef}
                type="text"
                id="user-input"
                placeholder="Type a message, /command, or drop files here..."
                autoFocus
                autoComplete="off"
                spellCheck="false"
                value={inputValue}
                onChange={handleChange}
                onKeyDown={handleKeyDown}
              />
            </div>
          </div>
        </div>
      </div>


      {/* Status bar */}
      <div id="status-bar" className="status-bar dim">
        <span id="git-status">
          {gitBranch && (<><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.5" className="icon-sm"><path d="M3 4v6a2 2 0 0 0 2 2h2M7 12l-2-2 2-2M11 5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM3 4.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z"/></svg>{gitBranch}{gitDirty ? '*' : ''}</>)}
        </span>
        <span className="bot-indicators">
          <span className="discord-label"><svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" className="icon-sm"><path d="M19.27 5.33C17.94 4.71 16.5 4.26 15 4a.09.09 0 0 0-.07.03c-.18.33-.39.76-.53 1.09a16.09 16.09 0 0 0-4.8 0c-.14-.34-.35-.76-.54-1.09-.01-.02-.04-.03-.07-.03-1.5.26-2.93.71-4.27 1.33-.01 0-.02.01-.03.02-2.72 4.07-3.47 8.03-3.1 11.95 0 .02.01.04.03.05 1.8 1.32 3.53 2.12 5.24 2.65.03.01.06 0 .07-.02.4-.55.76-1.13 1.07-1.74.02-.04 0-.08-.04-.09-.57-.22-1.11-.48-1.64-.78-.04-.02-.04-.08-.01-.11.11-.08.22-.17.33-.25.02-.02.05-.02.07-.01 3.44 1.57 7.15 1.57 10.55 0 .02-.01.05-.01.07.01.11.09.22.17.33.26.04.03.04.09-.01.11-.52.31-1.07.56-1.64.78-.04.01-.05.06-.04.09.32.61.68 1.19 1.07 1.74.03.01.06.02.09.01 1.72-.53 3.45-1.33 5.25-2.65.02-.01.03-.03.03-.05.44-4.53-.73-8.46-3.1-11.95-.01-.01-.02-.02-.04-.02zM8.52 14.91c-1.03 0-1.89-.95-1.89-2.12s.84-2.12 1.89-2.12c1.06 0 1.9.96 1.89 2.12 0 1.17-.84 2.12-1.89 2.12zm6.97 0c-1.03 0-1.89-.95-1.89-2.12s.84-2.12 1.89-2.12c1.06 0 1.9.96 1.89 2.12 0 1.17-.83 2.12-1.89 2.12z"/></svg>Discord</span>
          <span
            className={`bot-dot ${botStatus['mini-agent'] ? 'bot-on' : 'bot-off'}`}
            title={`mini-agent — ${botStatus['mini-agent'] ? 'click to stop' : 'click to start'}`}
            onClick={() => handleBotToggle('mini-agent')}
          >{botStatus['mini-agent'] !== undefined ? (botStatus['mini-agent'] ? 'on' : 'off') : '...'}</span>
          <span
            className={`bot-dot ${botStatus['emotion-game'] ? 'bot-on' : 'bot-off'}`}
            title={`emotion-game — ${botStatus['emotion-game'] ? 'click to stop' : 'click to start'}`}
            onClick={() => handleBotToggle('emotion-game')}
          >{botStatus['emotion-game'] !== undefined ? (botStatus['emotion-game'] ? 'on' : 'off') : '...'}</span>
        </span>
        {isLive && (
          <span id="live-indicator" onClick={handleCancel} title="Cancel"><svg viewBox="0 0 12 12" width="10" height="10" className="icon-sm"><circle cx="6" cy="6" r="4" fill="currentColor" className="live-dot"/></svg></span>
        )}
        {elapsedSec != null && (
          <span id="timer"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.5" className="icon-sm"><circle cx="8" cy="8" r="6.5"/><path d="M8 4.5V8l2.5 2"/></svg>{elapsedSec}s</span>
        )}
        <span id="theme-toggle" ref={themeToggleRef} onClick={() => setThemePickerOpen((p) => !p)} title={`Theme: ${themeEntry.name}`}>
          {PALETTE_SVG}
          {themePickerOpen && dropdownPos && (
            <div className="theme-dropdown" style={dropdownPos} onClick={(e) => e.stopPropagation()}>
              {THEMES.map((t) => (
                <div
                  key={t.id}
                  className={`theme-dropdown-item${t.id === theme ? ' theme-current' : ''}`}
                  onClick={(e) => { e.stopPropagation(); applyTheme(t.id); }}
                >
                  <span className="theme-icon">{t.icon}</span>
                  <span className="theme-name">{t.name}</span>
                  {t.id === theme && <span className="theme-check"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" className="icon-sm"><polyline points="3,8 6.5,11.5 13,5"/></svg></span>}
                </div>
              ))}
            </div>
          )}
        </span>
        {turnCountVal != null && (
          <span id="turn-counter"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.5" className="icon-sm"><path d="M2 8a6 6 0 0 1 6-6 5.5 5.5 0 0 1 5 3.5M14 8a6 6 0 0 1-6 6"/><polyline points="11,3 13,1 15,3"/></svg> turn <span id="turn-count">{turnCountVal}</span></span>
        )}
        {tokenCountVal != null && (
          <span id="token-counter"><svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.5" className="icon-sm"><circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="1.5" fill="currentColor"/></svg> <span id="token-count">{tokenCountVal}</span> tok</span>
        )}
        <div className="status-right">
          {restoredCount != null && (
            <span id="restored-info">restored {restoredCount} msgs</span>
          )}
          <span id="workspace-info" className="clickable" onClick={handleWorkspaceClick} title="Click to change workspace">{workspace}</span>
          <SessionPicker sessionName={sessionName} onSwitch={handleSessionSwitch} />
        </div>
      </div>
      {showSettings && <SettingsPanel onSaved={handleSettingsSaved} />}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Root export -- wraps App in Error Boundary
// ---------------------------------------------------------------------------
export default function App() {
  return (
    <ErrorBoundary>
      <AppShell />
    </ErrorBoundary>
  );
}
