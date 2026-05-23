import { useState, useMemo, useCallback } from 'react';
import Tree from 'react-d3-tree';

/**
 * AgentTree — hierarchical agent visualization using react-d3-tree.
 *
 * Converts our flat agent map to a nested tree that D3 lays out automatically.
 * Uses foreignObject for rich HTML node cards with status dots, colors, and hover effects.
 *
 * Props:
 *   agents: object { [task_id]: { name, desc, parent_id, toolCalls, thoughts, output, ok } }
 */

// ─── Data transformation: flat agent map → nested D3 tree ──────────────
function buildTreeData(agents) {
  const ids = Object.keys(agents);
  if (ids.length === 0) return null;

  // Group children by parent_id
  const childrenOf = new Map();
  for (const id of ids) {
    const agent = agents[id];
    const parent = agent.parent_id || 'orchestrator';
    if (!childrenOf.has(parent)) childrenOf.set(parent, []);
    childrenOf.get(parent).push(id);
  }

  function buildSubtree(taskId) {
    const agent = agents[taskId];
    const childIds = (childrenOf.get(taskId) || []).filter(id => agents[id]);
    return {
      name: agent?.name || taskId,
      attributes: {
        taskId,
        ok: agent?.ok,
        desc: agent?.desc || '',
        toolCalls: agent?.toolCalls || [],
        thoughts: agent?.thoughts || [],
        output: agent?.output || '',
      },
      children: childIds.map(buildSubtree),
    };
  }

  const rootChildIds = childrenOf.get('orchestrator') || [];
  return {
    name: '🧠 Orchestrator',
    attributes: {
      taskId: 'orchestrator',
      isOrchestrator: true,
      agentCount: ids.length,
    },
    children: rootChildIds.map(buildSubtree),
  };
}

// ─── Custom node renderer (foreignObject for rich HTML) ─────────────────
function renderForeignObjectNode({ nodeDatum, toggleNode, onNodeClick }) {
  const attrs = nodeDatum.attributes || {};
  const { taskId, isOrchestrator, ok, name, desc } = attrs;

  if (isOrchestrator) {
    return (
      <g>
        <foreignObject width={160} height={40} x={-80} y={-20}>
          <div
            xmlns="http://www.w3.org/1999/xhtml"
            style={{
              width: '100%', height: '100%',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              background: 'rgba(30, 30, 46, 0.95)',
              border: '2px solid var(--accent)',
              borderRadius: 8,
              color: 'var(--accent)',
              fontSize: 12,
              fontWeight: 700,
              fontFamily: 'var(--font-family)',
              cursor: 'pointer',
              boxSizing: 'border-box',
            }}
          >
            {nodeDatum.name}
          </div>
        </foreignObject>
      </g>
    );
  }

  // Sub-agent card
  const statusColor = ok === true ? 'var(--green)'
    : ok === false ? 'var(--red)'
    : 'var(--pulse)';
  const statusIcon = ok === true ? '✓'
    : ok === false ? '✗'
    : '●';
  const shortName = (name || taskId || '').slice(0, 18);
  const hasInfo = desc || (attrs.toolCalls?.length > 0);

  return (
    <g>
      {/* transparent hit area for hover/click */}
      <circle cx={0} cy={0} r={18} fill="transparent" style={{ cursor: 'pointer' }} />
      {/* card */}
      <foreignObject width={150} height={38} x={-75} y={-19}>
        <div
          xmlns="http://www.w3.org/1999/xhtml"
          style={{
            width: '100%', height: '100%',
            display: 'flex', alignItems: 'center',
            padding: '0 10px',
            gap: 8,
            background: 'rgba(49, 50, 68, 0.85)',
            border: `1.5px solid var(--border)`,
            borderRadius: 6,
            fontFamily: 'var(--font-family)',
            fontSize: 10,
            color: 'var(--accent)',
            cursor: 'pointer',
            boxSizing: 'border-box',
            transition: 'border-color 0.15s, background 0.15s',
            overflow: 'hidden',
            whiteSpace: 'nowrap',
          }}
        >
          {/* status dot */}
          <span style={{
            width: 7, height: 7, minWidth: 7,
            borderRadius: '50%',
            background: statusColor,
            display: 'inline-block',
          }} />
          {/* name */}
          <span style={{
            flex: 1,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            fontSize: 10,
          }}>{shortName}</span>
          {/* status icon */}
          <span style={{
            color: statusColor,
            fontSize: 11,
            fontWeight: 700,
            minWidth: 12,
            textAlign: 'right',
          }}>{statusIcon}</span>
        </div>
      </foreignObject>
    </g>
  );
}

// ─── Tooltip (rendered as overlay, positioned by mouse) ─────────────────
function Tooltip({ agent, position }) {
  if (!agent || !position) return null;

  const attrs = agent.attributes || {};
  const { taskId, desc, toolCalls, thoughts, output, ok } = attrs;

  return (
    <div
      className="agent-tree-tooltip"
      style={{
        left: position.x + 12,
        top: position.y - 10,
        position: 'fixed',
        zIndex: 1000,
        maxWidth: 320,
      }}
    >
      <div className="agent-tree-tooltip-header">
        <span className="accent">{agent.name || taskId}</span>
        <span className="dim" style={{ fontSize: 9, marginLeft: 6 }}>{taskId?.slice(0, 8)}</span>
      </div>

      {desc && (
        <div className="agent-tree-tooltip-section">
          <div className="agent-tree-tooltip-label">Task</div>
          <div className="agent-tree-tooltip-text">{desc.slice(0, 300)}</div>
        </div>
      )}

      {toolCalls && toolCalls.length > 0 && (
        <div className="agent-tree-tooltip-section">
          <div className="agent-tree-tooltip-label">Tool Calls ({toolCalls.length})</div>
          <div className="agent-tree-tooltip-text">
            {toolCalls.map((tc, i) => (
              <div key={i} className="agent-tree-tooltip-tool-line">
                <span className="accent">{tc.toolName}</span>
                {tc.toolArgs && <span className="dim"> {tc.toolArgs}</span>}
                {tc.ok != null && (
                  <span style={{ color: tc.ok ? 'var(--green)' : 'var(--red)', marginLeft: 4 }}>
                    {tc.ok ? 'OK' : 'ERR'}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {thoughts && thoughts.length > 0 && (
        <div className="agent-tree-tooltip-section">
          <div className="agent-tree-tooltip-label">Thoughts</div>
          <div className="agent-tree-tooltip-text agent-tree-tooltip-thoughts">
            {thoughts.slice(-10).map((t, i) => (
              <span key={i} className="thinking" style={{ display: 'block', fontSize: 10, lineHeight: 1.3 }}>{t}</span>
            ))}
          </div>
        </div>
      )}

      {output && (
        <div className="agent-tree-tooltip-section">
          <div className="agent-tree-tooltip-label">Output</div>
          <div className="agent-tree-tooltip-text" style={{ color: ok ? 'var(--green)' : 'var(--red)' }}>
            {output.slice(0, 500)}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Loading dots ───────────────────────────────────────────────────────
function LoadingDots() {
  return <span className="loading-dots"><span>.</span><span>.</span><span>.</span></span>;
}

// ─── Main component ────────────────────────────────────────────────────
export default function AgentTree({ agents }) {
  const [hoveredNode, setHoveredNode] = useState(null);
  const [tooltipPos, setTooltipPos] = useState(null);

  const treeData = useMemo(() => buildTreeData(agents), [agents]);
  const ids = Object.keys(agents);

  const handleNodeMouseOver = useCallback((nodeDatum, e) => {
    setHoveredNode(nodeDatum);
    setTooltipPos({ x: e.clientX, y: e.clientY });
  }, []);

  const handleNodeMouseMove = useCallback((e) => {
    setTooltipPos({ x: e.clientX, y: e.clientY });
  }, []);

  const handleNodeMouseOut = useCallback(() => {
    setHoveredNode(null);
    setTooltipPos(null);
  }, []);

  if (ids.length === 0 || !treeData) {
    return (
      <div className="agent-tree-empty">
        No active sub-agents
      </div>
    );
  }

  const running = ids.filter(id => agents[id]?.ok == null).length;
  const done = ids.filter(id => agents[id]?.ok != null).length;

  return (
    <div className="agent-tree-container">
      {/* Mini status bar */}
      <div className="agent-tree-status">
        <span className="dim" style={{ fontSize: 10 }}>
          {ids.length} agent{ids.length !== 1 ? 's' : ''}
          {running > 0 && (
            <span className="pulse" style={{ marginLeft: 6 }}>
              {running} running <LoadingDots />
            </span>
          )}
          {done > 0 && (
            <span style={{ color: 'var(--green)', marginLeft: 6 }}>
              {done} done
            </span>
          )}
        </span>
      </div>

      {/* D3 Tree */}
      <div className="agent-tree-svg-wrapper" style={{ width: '100%', height: 400 }}>
        <Tree
          data={treeData}
          orientation="vertical"
          pathFunc="step"
          translate={{ x: 200, y: 50 }}
          zoom={0.7}
          scaleExtent={{ min: 0.3, max: 2 }}
          separation={{ siblings: 1.5, nonSiblings: 2 }}
          nodeSize={{ x: 180, y: 70 }}
          renderCustomNodeElement={(rd3tProps) =>
            renderForeignObjectNode(rd3tProps)
          }
          onNodeMouseOver={handleNodeMouseOver}
          onNodeMouseMove={handleNodeMouseMove}
          onNodeMouseOut={handleNodeMouseOut}
          enableLegacyTransitions={false}
          collapsible={false}
          depthFactor={80}
        />
      </div>

      {/* Tooltip portal */}
      {hoveredNode && tooltipPos && (
        <Tooltip agent={hoveredNode} position={tooltipPos} />
      )}
    </div>
  );
}
