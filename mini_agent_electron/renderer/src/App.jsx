import { useState, useRef, useEffect, useCallback } from 'react';
import useSmoothStream from './hooks/useSmoothStream';
import { highlightSyntax } from './utils/syntax';

// ---------------------------------------------------------------------------
// Inline SVG icons (same as before)
// ---------------------------------------------------------------------------
const ICON_TOOL = `<svg class="tool-icon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>`;
const ICON_PARALLEL = `<svg class="tool-icon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="M2.6 12.08l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.91"/><path d="M2.6 18.08l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.91"/></svg>`;

// ---------------------------------------------------------------------------
// Character-level fade-in: renders text as spans, new chars animate in
// ---------------------------------------------------------------------------
function CharStream({ text, className = '' }) {
  return (
    <span className={className}>
      {[...text].map((ch, i) => (
        <span key={i} className="stream-char">{ch}</span>
      ))}
    </span>
  );
}

// ---------------------------------------------------------------------------
// A single log line
// ---------------------------------------------------------------------------
function LogLine({ line }) {
  if (line.html) {
    return <div className={line.cls || ''} dangerouslySetInnerHTML={{ __html: line.html }} />;
  }
  if (line.icon) {
    return <div className={line.cls || ''} dangerouslySetInnerHTML={{ __html: `${line.icon} ${line.text}` }} />;
  }
  return <div className={line.cls || ''}>{line.text}</div>;
}

// ---------------------------------------------------------------------------
// Auto-scrolling log container
// ---------------------------------------------------------------------------
function LogPanel({ id, className, lines, children }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines, children]);
  return (
    <div id={id} ref={ref} className={`log ${className || ''}`}>
      {lines && lines.map((line, i) => <LogLine key={i} line={line} />)}
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rounded frame wrapper for panels
// ---------------------------------------------------------------------------
function RoundedFrame({ id, title, children }) {
  return (
    <div id={id} className="panel rounded-frame">
      <div className="frame-top">
        <span className="border-char">╭</span>
        <span className="frame-title"> {title} </span>
        <span className="border-char border-fill">─</span>
        <span className="border-char">╮</span>
      </div>
      <div className="frame-body">
        <div className="frame-left"></div>
        <div className="frame-content">{children}</div>
        <div className="frame-right"></div>
      </div>
      <div className="frame-bottom">
        <span className="border-char">╰</span>
        <span className="border-char border-fill">─</span>
        <span className="border-char">╯</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
export default function App() {
  // Log state — arrays of { text, cls?, html?, icon? }
  const [toolsLines, setToolsLines] = useState([]);
  const [subagentLines, setSubagentLines] = useState([]);
  const [chatLines, setChatLines] = useState([]);

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

  const inputRef = useRef(null);
  const thinkingLogRef = useRef(null);
  const inThinkingRef = useRef(false);
  const submitTimeoutRef = useRef(null);

  // Helper to add a line to any log
  const addLine = useCallback((setter) => (line) => {
    setter((prev) => [...prev, line]);
  }, []);

  const addToolLine = useCallback((line) => addLine(setToolsLines)(line), [addLine]);
  const addSubLine = useCallback((line) => addLine(setSubagentLines)(line), [addLine]);

  // Setup listeners
  useEffect(() => {
    const api = window.miniAgent;
    if (!api) return;

    const unsubs = [];

    unsubs.push(api.on('backend:status', (data) => {
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
    }));

    // Fetch initial status (may have fired before we mounted)
    api.getStatus?.().then((data) => {
      if (!data) return;
      if (data.model != null) setModelName(data.model);
      if (data.session_name != null) setSessionName(data.session_name);
      if (data.workspace != null) setWorkspace(data.workspace);
      if (data.git_branch != null) {
        setGitBranch(data.git_branch);
        setGitDirty(!!data.git_dirty);
      }
      if (data.restored_count != null) setRestoredCount(data.restored_count);
    });

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
      thinking.flush();
    }));

    unsubs.push(api.on('stream:tool_start', (data) => {
      const icon = data.parallel ? ICON_PARALLEL : ICON_TOOL;
      addToolLine({ icon, text: data.summary, cls: 'dim' });
    }));

    unsubs.push(api.on('stream:tool_end', (data) => {
      const status = data.ok ? 'OK' : 'ERR';
      const cls = data.ok ? 'msg-tool-ok' : 'msg-tool-err';
      addToolLine({ text: `  ${status} ${data.detail}`, cls });
    }));

    unsubs.push(api.on('stream:tool_output', (data) => {
      const lines = data.line.split('\n');
      for (const line of lines) {
        if (line.trim()) {
          addToolLine({ html: highlightSyntax(`    ${line}`), cls: 'dim' });
        }
      }
    }));

    unsubs.push(api.on('stream:turn_complete', (data) => {
      clearTimeout(submitTimeoutRef.current);
      const agentText = chatStream.flush();
      if (agentText) {
        setChatLines((prev) => {
          const updated = [...prev];
          // replace last empty placeholder with agent response
          if (updated.length > 0 && updated[updated.length - 1].text === '') {
            updated[updated.length - 1] = { text: agentText, cls: 'msg-agent' };
          } else {
            updated.push({ text: agentText, cls: 'msg-agent' });
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
      setChatLines((prev) => [...prev, { text: `Error: ${data.message}`, cls: 'msg-error' }]);
      setIsLive(false);
      setInputDisabled(false);
      inputRef.current?.focus();
    }));

    unsubs.push(api.on('backend:response', (data) => {
      if (data.lines) {
        for (const line of data.lines) {
          if (data.target === 'chat') {
            setChatLines((prev) => [...prev, { text: line, cls: 'msg-status' }]);
          } else {
            addToolLine({ text: line, cls: 'dim' });
          }
        }
      }
    }));

    return () => unsubs.forEach((fn) => fn());
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Submit handler
  const handleSubmit = useCallback((text) => {
    if (!text || inputDisabled) return;

    if (text.startsWith('/')) {
      window.miniAgent.command(text);
      inputRef.current.value = '';
      return;
    }

    setChatLines((prev) => [
      ...prev,
      { text, cls: 'msg-user' },
      { text: '', cls: '' },
      { text: '', cls: '' },
    ]);
    chatStream.reset();

    setIsLive(true);
    setInputDisabled(true);
    inputRef.current.value = '';

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

  // Auto-scroll thinking log
  useEffect(() => {
    if (thinkingLogRef.current) {
      thinkingLogRef.current.scrollTop = thinkingLogRef.current.scrollHeight;
    }
  }, [thinking.displayedText]);

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
      <div className="hr" />

      {/* Body: two panels */}
      <div id="body-panels">
        {/* Left pane: Tools & Thinking */}
        <RoundedFrame id="left-pane" title="Tools &amp; Thinking">
          <LogPanel id="tools-log" className="scrollable dim" lines={toolsLines} />
          <div className="hr" />
          <div id="thinking-log" ref={thinkingLogRef} className="log thinking-log thinking">
            {thinking.displayedText && (
              <CharStream text={thinking.displayedText} className="msg-thinking" />
            )}
          </div>
          <div className="hr" />
          <div className="sub-label dim"> Sub-agents</div>
          <LogPanel id="subagents-log" className="subagents-log dim" lines={subagentLines} />
        </RoundedFrame>

        {/* Right pane: Chat */}
        <RoundedFrame id="right-pane" title="Chat">
          <div id="chat-log" className="log scrollable text">
            {chatLines.map((line, i) => <LogLine key={`line-${i}`} line={line} />)}
            {chatStream.displayedText && (
              <CharStream text={chatStream.displayedText} className="msg-agent" />
            )}
          </div>
        </RoundedFrame>
      </div>

      {/* Input */}
      <div id="input-frame" className="rounded-frame">
        <div className="frame-top">
          <span className="border-char">╭</span>
          <span className="border-char border-fill">─ Input ─</span>
          <span className="border-char border-fill">─</span>
          <span className="border-char">╮</span>
        </div>
        <div className="frame-body">
          <div className="frame-left"></div>
          <div className="frame-content">
            <div id="input-container">
              <span className="prompt">{'>'}</span>
              <input
                ref={inputRef}
                type="text"
                id="user-input"
                placeholder="Type a message or /command..."
                autoFocus
                autoComplete="off"
                spellCheck="false"
                disabled={inputDisabled}
                onKeyDown={handleKeyDown}
              />
            </div>
          </div>
          <div className="frame-right"></div>
        </div>
        <div className="frame-bottom">
          <span className="border-char">╰</span>
          <span className="border-char border-fill">─</span>
          <span className="border-char">╯</span>
        </div>
      </div>

      <div className="hr" />

      {/* Status bar */}
      <div id="status-bar" className="status-bar dim">
        <span id="git-status">
          {gitBranch && `⎇ ${gitBranch}${gitDirty ? '*' : ''}`}
        </span>
        {isLive && (
          <span id="live-indicator" onClick={() => window.miniAgent?.cancel()} title="Cancel"> ●</span>
        )}
        {turnCountVal != null && (
          <span id="turn-counter"> ↻ turn <span id="turn-count">{turnCountVal}</span></span>
        )}
        {tokenCountVal != null && (
          <span id="token-counter"> ⊙ <span id="token-count">{tokenCountVal}</span> tok</span>
        )}
        <div className="status-right">
          {restoredCount != null && (
            <span id="restored-info">restored {restoredCount} msgs</span>
          )}
          <span id="workspace-info">{workspace}</span>
          <span id="header-session" className="dim">{sessionName}</span>
        </div>
      </div>
    </div>
  );
}
