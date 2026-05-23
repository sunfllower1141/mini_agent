import { useState, useRef, useEffect, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Highlight, themes } from 'prism-react-renderer';
import useSmoothStream from './hooks/useSmoothStream';


// ---------------------------------------------------------------------------
// Inline SVG icons (same as before)
// ---------------------------------------------------------------------------



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
// Syntax-highlighted code block using prism-react-renderer
// ---------------------------------------------------------------------------
const LANG_MAP = {
  py: 'python', js: 'javascript', jsx: 'javascript', ts: 'typescript',
  tsx: 'typescript', json: 'json', css: 'css', html: 'html', sh: 'bash',
  bash: 'bash', zsh: 'bash', yaml: 'yaml', yml: 'yaml', toml: 'toml',
  md: 'markdown', sql: 'sql', rs: 'rust', go: 'go', java: 'java',
  c: 'c', cpp: 'cpp', h: 'c', rb: 'ruby', Makefile: 'makefile',
};

function guessLang(toolName, filePath) {
  if (toolName === 'run_shell') return 'bash';
  if (filePath) {
    const ext = filePath.split('.').pop().toLowerCase();
    if (LANG_MAP[ext]) return LANG_MAP[ext];
    const base = filePath.split('/').pop();
    if (LANG_MAP[base]) return LANG_MAP[base];
  }
  return 'text';
}

function CodeBlock({ code, language = 'text' }) {
  return (
    <div className="code-block">
      <Highlight theme={themes.palenight} code={code} language={language}>
        {({ style, tokens, getLineProps, getTokenProps }) => (
          <pre style={{ ...style, background: 'transparent', margin: 0, padding: '4px 0' }}>
            {tokens.map((line, i) => (
              <div key={i} {...getLineProps({ line })}>
                {line.map((token, key) => (
                  <span key={key} {...getTokenProps({ token })} />
                ))}
              </div>
            ))}
          </pre>
        )}
      </Highlight>
    </div>
  );
}

// ---------------------------------------------------------------------------
// A single log line — supports plain text, icons, HTML, and markdown
// ---------------------------------------------------------------------------
function LogLine({ line }) {
  if (line.component) {
    return <div className={line.cls || ''}>{line.component}</div>;
  }
  if (line.html) {
    return <div className={line.cls || ''} dangerouslySetInnerHTML={{ __html: line.html }} />;
  }
  if (line.icon) {
    return <div className={line.cls || ''} dangerouslySetInnerHTML={{ __html: `${line.icon} ${line.text}` }} />;
  }
  if (line.markdown) {
    return (
      <div className={`md-line ${line.cls || ''}`}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          allowDangerousHtml={true}
          components={{
            p: ({ children }) => <span>{children}</span>,
          }}
        >
          {line.text}
        </ReactMarkdown>
      </div>
    );
  }
  // Plain text — if it contains SVG icons (from emoji replacement), render as HTML
  if (line.text && line.text.includes('<svg')) {
    return <div className={line.cls || ''} dangerouslySetInnerHTML={{ __html: line.text }} />;
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
  const [thinkingOutput, setThinkingOutput] = useState('');

  const inputRef = useRef(null);
  const thinkingLogRef = useRef(null);
  const chatLogRef = useRef(null);
  const inThinkingRef = useRef(false);
  const submitTimeoutRef = useRef(null);
  const toolOutputBuf = useRef([]);
  const toolOutputLang = useRef('text');
  const toolLinesRef = useRef(null);

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
      setThinkingOutput('');
    }));

    unsubs.push(api.on('stream:thinking_end', () => {
      inThinkingRef.current = false;
      setThinkingOutput(thinking.flush());
    }));

    unsubs.push(api.on('stream:tool_start', (data) => {
      addToolLine({ text: '', cls: 'tool-separator' });
      // Color-code the tool name
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
        html: `<span class="accent">${toolName}</span><span class="dim">${toolArgs}</span>`,
        cls: '',
      });
      // Reset output buffer for this tool call
      toolOutputBuf.current = [];
      toolOutputLang.current = guessLang(toolName, toolArgs);
    }));

    unsubs.push(api.on('stream:tool_output', (data) => {
      const lines = data.line.split('\n');
      for (const line of lines) {
        toolOutputBuf.current.push(line);
      }
    }));

    unsubs.push(api.on('stream:tool_end', (data) => {
      const status = data.ok ? 'OK' : 'ERR';
      const cls = data.ok ? 'msg-tool-ok' : 'msg-tool-err';
      addToolLine({ text: `  ${status} ${data.detail}`, cls });
      // Add syntax-highlighted output block
      const code = toolOutputBuf.current.join('\n').trim();
      if (code) {
        addToolLine({
          component: <CodeBlock code={code} language={toolOutputLang.current} />,
          cls: '',
        });
      }
      toolOutputBuf.current = [];
    }));

    unsubs.push(api.on('stream:turn_complete', (data) => {
      clearTimeout(submitTimeoutRef.current);
      const agentText = chatStream.flush();
      if (agentText) {
        setChatLines((prev) => {
          const updated = [...prev];
          if (updated.length > 0 && updated[updated.length - 1].cls === 'msg-agent-pending') {
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
      if (data.lines && data.target === 'chat') {
        for (const line of data.lines) {
          setChatLines((prev) => [...prev, { text: line, cls: 'msg-status' }]);
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
      ...(prev.length > 0 ? [{ text: '', cls: 'msg-separator' }] : []),
      { text, cls: 'msg-user' },
      { text: '', cls: 'msg-separator' },
      { text: '', cls: 'msg-agent-pending' },
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

  // Cancel handler — immediately reset UI, then tell backend
  const handleCancel = useCallback(() => {
    window.miniAgent?.cancel();
    clearTimeout(submitTimeoutRef.current);
    inThinkingRef.current = false;
    const agentText = chatStream.flush();
    const thinkText = thinking.flush();
    if (thinkText) setThinkingOutput(thinkText);
    thinking.reset();
    if (agentText) {
      setChatLines((prev) => {
        const updated = [...prev];
        if (updated.length > 0 && updated[updated.length - 1].cls === 'msg-agent-pending') {
          updated[updated.length - 1] = { text: agentText, cls: 'msg-agent' };
        }
        return updated;
      });
      chatStream.reset();
    }
    setIsLive(false);
    setInputDisabled(false);
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
            {thinkingOutput && !thinking.displayedText && (
              <div className="msg-thinking">
                <ReactMarkdown remarkPlugins={[remarkGfm]} allowDangerousHtml={true}
>{thinkingOutput}</ReactMarkdown>
              </div>
            )}
          </div>
          <div className="hr" />
          <div className="sub-label dim"> Sub-agents</div>
          <LogPanel id="subagents-log" className="subagents-log dim" lines={subagentLines} />
        </RoundedFrame>

        {/* Right pane: Chat */}
        <RoundedFrame id="right-pane" title="Chat">
          <div id="chat-log" ref={chatLogRef} className="log scrollable text">
            {chatLines.map((line, i) => {
              if (line.cls === 'msg-agent') {
                return (
                  <div key={`line-${i}`} className="msg-agent">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} allowDangerousHtml={true}
>
                      {line.text}
                    </ReactMarkdown>
                  </div>
                );
              }
              return <LogLine key={`line-${i}`} line={line} />;
            })}
            {chatStream.displayedText && (
              <div className="msg-agent">
                <ReactMarkdown remarkPlugins={[remarkGfm]} allowDangerousHtml={true}
>
                  {chatStream.displayedText}
                </ReactMarkdown>
              </div>
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


      {/* Status bar */}
      <div id="status-bar" className="status-bar dim">
        <span id="git-status">
          {gitBranch && `⎇ ${gitBranch}${gitDirty ? '*' : ''}`}
        </span>
        {isLive && (
          <span id="live-indicator" onClick={handleCancel} title="Cancel"> ●</span>
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
