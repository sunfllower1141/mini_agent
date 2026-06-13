import { useRef, useEffect } from 'react';

/**
 * SubAgentsPane -- displays active sub-agents in a stacked layout.
 *
 * Each sub-agent gets a section split into 3 horizontal rows:
 *   Top:    Tool calls (what tools the sub-agent used)
 *   Middle: Messages to the sub-agent (task description)
 *   Bottom: Thinking stream + final output
 *
 * Props:
 *   agents: object { [task_id]: { name, desc, toolCalls, thoughts, output, ok } }
 */

function escapeHtml(text) {
  if (!text) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function SubAgentSection({ agent }) {
  const thoughtRef = useRef(null);
  const toolRef = useRef(null);

  // Auto-scroll after DOM paints (requestAnimationFrame avoids race with render)
  useEffect(() => {
    const el = thoughtRef.current;
    if (el) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
  }, [agent.thoughts]);
  useEffect(() => {
    const el = toolRef.current;
    if (el) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
  }, [agent.toolCalls]);

  const statusIcon = agent.ok === true ? 'V' : agent.ok === false ? 'X' : '*';
  const statusCls = agent.ok === true ? 'subagent-ok' : agent.ok === false ? 'subagent-err' : 'subagent-active';

  return (
    <div className="subagent-section">
      {/* Header */}
      <div className="subagent-header">
        <span className={statusCls}>{statusIcon}</span>
        <span className="subagent-name">{agent.name || agent.task_id}</span>
        <span className="dim" style={{ fontSize: '10px', marginLeft: 'auto' }}>{agent.task_id}</span>
      </div>

      {/* Row 1: Tool calls */}
      <div className="subagent-row subagent-tools">
        <div className="subagent-row-label dim">Tool Calls</div>
        <div className="subagent-row-content log" ref={toolRef}>
          {agent.toolCalls.length === 0 && !agent.ok && (
            <div className="dim" style={{ padding: '2px 0' }}>Waiting...</div>
          )}
          {agent.toolCalls.map((tc, i) => (
            <div key={i} className="subagent-tool-line">
              {tc.toolName ? (
                <>
                  <span className="accent">{tc.toolName}</span>
                  {tc.toolArgs && <span className="dim">{tc.toolArgs}</span>}
                </>
              ) : (
                <span className="dim">{escapeHtml(tc.summary)}</span>
              )}
              {tc.result && (
                <span className={tc.ok ? 'dim' : 'msg-tool-err'} style={{ marginLeft: '4px' }}>
                  {tc.ok ? 'OK' : 'ERR'}
                </span>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Row 2: Messages (task description) */}
      <div className="subagent-row subagent-messages">
        <div className="subagent-row-label dim">Task</div>
        <div className="subagent-row-content" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
          {agent.desc ? escapeHtml(agent.desc) : <span className="dim">No task description</span>}
        </div>
      </div>

      {/* Row 3: Thinking + Output */}
      <div className="subagent-row subagent-thoughts">
        <div className="subagent-row-label dim">Thoughts &amp; Output</div>
        <div className="subagent-row-content log" ref={thoughtRef}>
          {agent.thoughts.length === 0 && !agent.output && !agent.ok && (
            <div className="dim" style={{ padding: '2px 0' }}>Waiting...</div>
          )}
          {agent.thoughts.map((t, i) => (
            <span key={i} className="thinking thought-chunk">{t}</span>
          ))}
          {agent.output && (
            <div className={agent.ok ? 'subagent-ok' : 'subagent-err'} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {escapeHtml(agent.output)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function SubAgentsPane({ agents }) {
  const ids = Object.keys(agents);

  if (ids.length === 0) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        height: '100%', color: 'var(--dim)', fontSize: '12px',
      }}>
        No active sub-agents
      </div>
    );
  }

  return (
    <div className="subagents-pane">
      {ids.map((taskId) => (
        <SubAgentSection key={taskId} agent={{ task_id: taskId, ...agents[taskId] }} />
      ))}
    </div>
  );
}
