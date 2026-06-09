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

// ---------------------------------------------------------------------------
// SVG icons — minimalistic Lucide-style, matching emoji_svg.py convention
// ---------------------------------------------------------------------------
// SVG icons — minimalistic Lucide-style, matching emoji_svg.py convention.
// Defined as functions so React gets fresh elements each render.
const SVG = {
  moon:      () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>,
  sun:       () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>,
  droplet:   () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/></svg>,
  snowflake: () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/><path d="m20 16-4-4 4-4"/><path d="m4 8 4 4-4 4"/><path d="m16 4-4 4-4-4"/><path d="m8 20 4-4 4 4"/></svg>,
  coffee:    () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8h1a4 4 0 0 1 0 8h-1"/><path d="M2 8h16v9a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V8z"/><line x1="6" y1="1" x2="6" y2="4"/><line x1="10" y1="1" x2="10" y2="4"/><line x1="14" y1="1" x2="14" y2="4"/></svg>,
  flower:    () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M4.93 7.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 16.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>,
  layers:    () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="12 2 22 8.5 22 15.5 12 22 2 15.5 2 8.5 12 2"/><line x1="12" y1="22" x2="12" y2="15.5"/><polyline points="22 8.5 12 15.5 2 8.5"/></svg>,
  circleHalf: () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2a10 10 0 1 0 0 20V2z"/></svg>,
  moonStars: () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M16 3a5 5 0 0 0 0 10"/><path d="M6 7l1 1M9 10l-1-1M18 17l1 1M21 20l-1-1M3 17l3-3"/><circle cx="8" cy="17" r="2"/><path d="M21 12.79A9 9 0 1 1 11.21 3"/></svg>,
  palette:   () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="13.5" cy="6.5" r="1.5" fill="currentColor"/><circle cx="17.5" cy="10.5" r="1.5" fill="currentColor"/><circle cx="8.5" cy="7.5" r="1.5" fill="currentColor"/><circle cx="6.5" cy="12.5" r="1.5" fill="currentColor"/><path d="M12 2C6.49 2 2 6.49 2 12s4.49 10 10 10a2 2 0 0 0 2-2c0-.52-.2-1-.53-1.37-.33-.36-.47-.83-.47-1.3 0-1.1.9-2 2-2h2.35c3.52 0 6.35-2.83 6.35-6.35C23.7 5.27 19.73 2 12 2z"/></svg>,
  gitBranch: () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>,
  circleFill: () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><circle cx="12" cy="12" r="6"/></svg>,
  refresh:   () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>,
  circleDot: () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>,
  check:     () => <svg className="svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>,
};

// Theme registry — name, data-theme value, status-bar icon
const THEMES = [
  { name: 'Dark',         id: 'dark',         icon: SVG.moon },
  { name: 'Light',        id: 'light',        icon: SVG.sun },
  { name: 'Dracula',      id: 'dracula',      icon: SVG.droplet },
  { name: 'Nord',         id: 'nord',         icon: SVG.snowflake },
  { name: 'Catppuccin',   id: 'catppuccin',   icon: SVG.coffee },
  { name: 'Rosé Pine',    id: 'rose-pine',    icon: SVG.flower },
  { name: 'Gruvbox',      id: 'gruvbox',      icon: SVG.layers },
  { name: 'Solarized',    id: 'solarized',    icon: SVG.circleHalf },
  { name: 'Tokyo Night',  id: 'tokyo-night',  icon: SVG.moonStars },
  { name: 'Monokai',      id: 'monokai',      icon: SVG.palette },
];

function setThemeDom(id) {
  document.documentElement.setAttribute('data-theme', id);
  localStorage.setItem('mini_agent_theme', id);
}


// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
function AppShell() {
  // Log state — arrays of { text, cls?, html?, icon? }
  const [toolsLines, setToolsLines] = useState([]);
  const [chatLines, setChatLines] = useState([]);

  // Sub-agent data — { [task_id]: { name, desc, toolCalls: [], thoughts: [], output: "", ok: null } }
  const [subagentData, setSubagentData] = useState({});

  // Smooth streaming for thinking & chat
  const thinking = useSmoothStream({ speed: 10 });
  const chatStream = useSmoothStream({ speed: 8 });

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
  const [inputDisabled, setInputDisabled] = useState(false);
  const [thinkingBlocks, setThinkingBlocks] = useState([]);

  const inputRef = useRef(null);
  const thinkingLogRef = useRef(null);
  const chatLogRef = useRef(null);
  const inThinkingRef = useRef(false);
  const submitTimeoutRef = useRef(null);
  const toolOutputStack = useRef([]); // stack of buffers for parallel tool calls
  const lineIdRef = useRef(0); // monotonically increasing ID for stable React keys
  const themeToggleRef = useRef(null);
  const nextLineId = useCallback(() => ++lineIdRef.current, []);
  const [showSettings, setShowSettings] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const [theme, setTheme] = useState(() => localStorage.getItem('mini_agent_theme') || 'dark');
  const [themePickerOpen, setThemePickerOpen] = useState(false);

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

  // Native click listener on theme toggle (bypasses React synthetic events)
  useEffect(() => {
    const el = themeToggleRef.current;
    if (!el) return;
    const handler = (e) => {
      e.preventDefault();
      e.stopPropagation();
      setThemePickerOpen((p) => !p);
    };
    el.addEventListener('click', handler, { capture: true });
    return () => {
      el.removeEventListener('click', handler, { capture: true });
    };
  }, []);

  // Helper to add a line to any log
  const addLine = useCallback((setter) => (line) => {
    setter((prev) => [...prev, line]);
  }, []);

  const addToolLine = useCallback((line) => addLine(setToolsLines)(line), [addLine]);

  // Status / init — fetched once on mount (empty deps to avoid re-render loop)
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
        // Backend came online — hide settings if it was showing
        setShowSettings(false);
      }
      if (data.model != null) setModelName(data.model);
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
      setIsLive(false);
      setInputDisabled(false);
      inputRef.current?.focus();
    }));

    unsubs.push(api.on('stream:error', (data) => {
      clearTimeout(submitTimeoutRef.current);
      chatStream.flush();
      chatStream.reset();
      setChatLines((prev) => [...prev, { id: nextLineId(), text: `Error: ${data.message}`, cls: 'msg-error' }]);
      setIsLive(false);
      setInputDisabled(false);
      inputRef.current?.focus();
    }));

    unsubs.push(api.on('backend:response', (data) => {
      if (data.lines && data.target === 'chat') {
        for (const line of data.lines) {
          setChatLines((prev) => [...prev, { id: nextLineId(), text: line, cls: 'msg-status' }]);
        }
      }
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
          // `/theme <name>` — fuzzy match against theme id or name
          const match = THEMES.find((t) =>
            t.id.toLowerCase() === arg.toLowerCase() ||
            t.name.toLowerCase() === arg.toLowerCase()
          );
          if (match) {
            applyTheme(match.id);
          }
        } else {
          // `/theme` with no arg — cycle
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

    // Safety timeout — re-enable after 120s
    submitTimeoutRef.current = setTimeout(() => {
      setIsLive(false);
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

  // Settings saved handler — backend will send backend:status { ready: true }
  // which triggers setShowSettings(false) in the onStatus listener
  const handleSettingsSaved = useCallback(() => {
    // Let the backend:status event handle hiding the panel
  }, []);

  // Cancel handler — immediately reset UI, then tell backend
  const handleCancel = useCallback(() => {
    window.miniAgent?.cancel();
    clearTimeout(submitTimeoutRef.current);
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
  }, [chatStream, thinking]);

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

  return (
    <div id="app">
      {/* Header */}
      <div id="header" className="header">
        <span className="dim"> mini_agent — </span>
        <span id="header-model" className="text">{modelName}</span>
      </div>

      {/* Body: three panels */}
      <div id="body-panels">
        {/* Left stack: Tools & Thinking + Agent Tree */}
        <div id="left-stack">
          <RoundedFrame id="left-pane" title="Tools &amp; Thinking">
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
        <RoundedFrame id="right-pane" title="Chat">
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
      <div id="input-frame" className="rounded-frame">
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
          {gitBranch && <>{SVG.gitBranch()} {gitBranch}{gitDirty ? '*' : ''}</>}
        </span>
        {isLive && (
          <span id="live-indicator" onClick={handleCancel} title="Cancel"> {SVG.circleFill()}</span>
        )}
        <button id="theme-toggle" ref={themeToggleRef} onClick={() => setThemePickerOpen((p) => !p)} title={`Theme: ${themeEntry.name}`}>
          {themeEntry.icon()}
        </button>
        {themePickerOpen && (
          <div className="theme-dropdown">
            {THEMES.map((t) => (
              <div
                key={t.id}
                className={`theme-dropdown-item${t.id === theme ? ' theme-current' : ''}`}
                onClick={() => applyTheme(t.id)}
              >
                <span className="theme-icon">{t.icon()}</span>
                <span className="theme-name">{t.name}</span>
                {t.id === theme && <span className="theme-check">{SVG.check()}</span>}
              </div>
            ))}
          </div>
        )}
        {turnCountVal != null && (
          <span id="turn-counter"> {SVG.refresh()} turn <span id="turn-count">{turnCountVal}</span></span>
        )}
        {tokenCountVal != null && (
          <span id="token-counter"> {SVG.circleDot()} <span id="token-count">{tokenCountVal}</span> tok</span>
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
// Root export — wraps App in Error Boundary
// ---------------------------------------------------------------------------
export default function App() {
  return (
    <ErrorBoundary>
      <AppShell />
    </ErrorBoundary>
  );
}
