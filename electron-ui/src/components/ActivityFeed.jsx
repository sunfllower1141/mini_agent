import React, { useRef, useEffect } from 'react';

/**
 * ActivityFeed — scrollable log of agent events (tool.start, tool.result,
 * heartbeats) with auto-scroll to bottom.
 */
export default function ActivityFeed({ activities }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [activities.length]);

  if (!activities || activities.length === 0) {
    return (
      <div className="activity-feed" style={{ justifyContent: 'center', alignItems: 'center' }}>
        <div className="empty-state" style={{ height: 'auto' }}>
          <div className="icon" style={{ fontSize: 24 }}>📡</div>
          <div className="subtitle">Agent activity will appear here in real time.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="activity-feed">
      {activities.map((entry) => (
        <ActivityEntry key={entry.id} entry={entry} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}

function ActivityEntry({ entry }) {
  const { type, toolName, filePath, agentId, success, summary, ts } = entry;

  // Determine indicator class
  let indicatorClass = 'token';
  if (type === 'tool.start') indicatorClass = 'start';
  else if (type === 'tool.result' && success) indicatorClass = 'result-ok';
  else if (type === 'tool.result' && !success) indicatorClass = 'result-fail';
  else if (type === 'heartbeat') indicatorClass = 'heartbeat';

  const time = new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const agentColors = ['#58a6ff', '#f0883e', '#bc8cff', '#3fb950'];
  const agentColor = agentId ? agentColors[agentId.charCodeAt(0) % agentColors.length] : 'var(--text-muted)';

  return (
    <div className="activity-entry">
      <div className={`indicator ${indicatorClass}`} />
      <div className="body">
        {agentId && (
          <span className="agent-tag" style={{ background: agentColor + '22', color: agentColor }}>
            {agentId}
          </span>
        )}
        {type === 'tool.start' && (
          <>
            <span className="tool-name">{toolName}</span>
            {filePath && <span className="file-path">📄 {shortPath(filePath)}</span>}
          </>
        )}
        {type === 'tool.result' && (
          <>
            <span className="tool-name" style={{ color: success ? 'var(--success)' : 'var(--danger)' }}>
              {success ? '✓' : '✗'} {toolName}
            </span>
            {filePath && <span className="file-path">📄 {shortPath(filePath)}</span>}
            {summary && <span className="summary">{truncate(summary, 120)}</span>}
          </>
        )}
        {type === 'heartbeat' && (
          <span style={{ color: 'var(--text-muted)' }}>
            💓 {agentId} heartbeat — {entry.status || 'alive'}
          </span>
        )}
      </div>
      <div className="timestamp">{time}</div>
    </div>
  );
}

function shortPath(path) {
  const parts = path.split('/');
  if (parts.length <= 2) return path;
  return '.../' + parts.slice(-2).join('/');
}

function truncate(text, max) {
  if (!text) return '';
  if (text.length <= max) return text;
  return text.slice(0, max) + '…';
}
