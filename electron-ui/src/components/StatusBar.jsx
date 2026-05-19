import React from 'react';

/**
 * StatusBar — bottom bar showing connection state, agent count,
 * activity stats.
 */
export default function StatusBar({ connected, agentCount, activityCount, nodeCount }) {
  const dotClass = connected ? 'connected' : 'disconnected';
  const statusText = connected ? 'Connected' : 'Disconnected';

  return (
    <div className="status-bar">
      <div className="status-left">
        <span className={`status-dot ${dotClass}`} />
        <span>{statusText}</span>
        <span className="status-stat">
          Agents: <span>{agentCount}</span>
        </span>
      </div>
      <div className="status-right">
        <span className="status-stat">
          Events: <span>{activityCount}</span>
        </span>
        <span className="status-stat">
          Files: <span>{nodeCount}</span>
        </span>
      </div>
    </div>
  );
}
