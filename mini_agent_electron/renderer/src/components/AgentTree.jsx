import { useMemo, useCallback, useEffect, useState, useRef } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  useReactFlow,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import ELK from 'elkjs/lib/elk.bundled.js';

const elk = new ELK();

/**
 * AgentTree — hierarchical agent visualization using React Flow + elkjs.
 *
 * Converts our flat agent map into an auto-laid-out node/edge graph.
 * Real React components for subagent cards (no SVG foreignObject hack).
 *
 * Props:
 *   agents: object { [task_id]: { name, desc, parent_id, toolCalls, thoughts, output, ok } }
 */

// ─── ELK graph builder (flat agent map → nested ELK tree) ────────────────────
function buildElkTree(agents) {
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
    const childIds = (childrenOf.get(taskId) || []).filter(id => agents[id]);
    return {
      id: taskId,
      width: 160,
      height: 40,
      children: childIds.map(buildSubtree),
    };
  }

  return {
    id: '__root__',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'DOWN',
      'elk.spacing.nodeNode': '50',
      'elk.layered.spacing.nodeNodeBetweenLayers': '70',
      'elk.edgeRouting': 'ORTHOGONAL',
    },
    children: [
      {
        id: 'orchestrator',
        width: 170,
        height: 48,
        children: (childrenOf.get('orchestrator') || []).map(buildSubtree),
      },
    ],
  };
}

// ─── Agent node card (React component — no foreignObject!) ──────────────────
function AgentNode({ data }) {
  const { agent, isOrchestrator } = data;

  if (isOrchestrator) {
    return (
      <div className="agent-rf-node agent-rf-node--orch">
        🧠 Orchestrator
      </div>
    );
  }

  const ok = agent?.ok;
  const statusColor = ok === true ? 'var(--green)'
    : ok === false ? 'var(--red)'
    : 'var(--pulse)';
  const statusIcon = ok === true ? '✓'
    : ok === false ? '✗'
    : '●';
  const shortName = (agent?.name || '').slice(0, 18);
  const toolCount = agent?.toolCalls?.length || 0;

  return (
    <div className="agent-rf-node agent-rf-node--sub">
      <span
        className="agent-rf-node__dot"
        style={{ background: statusColor }}
      />
      <span className="agent-rf-node__name">{shortName}</span>
      {toolCount > 0 && (
        <span className="agent-rf-node__tools dim">{toolCount}t</span>
      )}
      <span
        className="agent-rf-node__status"
        style={{ color: statusColor }}
      >{statusIcon}</span>
    </div>
  );
}

const nodeTypes = { agentNode: AgentNode };

// ─── Tooltip (positioned via portal over ReactFlow) ─────────────────────────
function Tooltip({ agent, position }) {
  if (!agent || !position) return null;

  const { taskId, desc, toolCalls, thoughts, output, ok, name } = agent.attributes || agent;

  return (
    <div
      className="agent-tree-tooltip"
      style={{
        left: position.x + 14,
        top: position.y - 10,
        position: 'fixed',
        zIndex: 1000,
        maxWidth: 320,
      }}
    >
      <div className="agent-tree-tooltip-header">
        <span className="accent">{name || taskId}</span>
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

// ─── Loading dots ───────────────────────────────────────────────────────────
function LoadingDots() {
  return <span className="loading-dots"><span>.</span><span>.</span><span>.</span></span>;
}

// ══════════════════════════════════════════════════════════════════════════════
// Main component
// ══════════════════════════════════════════════════════════════════════════════
export default function AgentTree({ agents }) {
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [hoveredAgent, setHoveredAgent] = useState(null);
  const [tooltipPos, setTooltipPos] = useState(null);

  const { fitView } = useReactFlow();
  const prevCountRef = useRef(0);

  const ids = Object.keys(agents);

  // Run elkjs layout whenever the agents map changes
  useEffect(() => {
    const elkTree = buildElkTree(agents);
    if (!elkTree) {
      setNodes([]);
      setEdges([]);
      return;
    }

    let cancelled = false;

    elk.layout(elkTree).then((layout) => {
      if (cancelled) return;

      const newNodes = [];
      const newEdges = [];

      function walk(node, parentId) {
        newNodes.push({
          id: node.id,
          position: { x: node.x || 0, y: node.y || 0 },
          data: {
            agent: node.id === 'orchestrator' ? null : agents[node.id],
            isOrchestrator: node.id === 'orchestrator',
          },
          type: 'agentNode',
          sourcePosition: 'bottom',
          targetPosition: 'top',
          draggable: false,
        });

        if (parentId) {
          const childAgent = agents[node.id];
          const isRunning = childAgent?.ok == null;
          newEdges.push({
            id: `${parentId}->${node.id}`,
            source: parentId,
            target: node.id,
            type: 'smoothstep',
            animated: isRunning,
            style: {
              stroke: isRunning ? 'var(--pulse)' : 'var(--border)',
              strokeWidth: isRunning ? 2 : 1,
            },
          });
        }

        if (node.children) {
          for (const child of node.children) {
            walk(child, node.id);
          }
        }
      }

      for (const child of layout.children || []) {
        walk(child, null);
      }

      setNodes(newNodes);
      setEdges(newEdges);

      // Auto-fit on first load or when agent count changes
      if (prevCountRef.current !== ids.length) {
        prevCountRef.current = ids.length;
        // Schedule fitView after React renders the new nodes
        requestAnimationFrame(() => {
          fitView({ padding: 0.2, duration: 300 });
        });
      }
    });

    return () => { cancelled = true; };
  }, [agents, setNodes, setEdges, fitView, ids.length]);

  // ── Event handlers ────────────────────────────────────────────────────────
  const onNodeMouseEnter = useCallback((event, node) => {
    const agent = agents[node.id];
    if (!agent) return;
    setHoveredAgent({
      attributes: {
        ...agent,
        taskId: node.id,
        name: agent.name || node.id,
        ok: agent.ok,
      },
      name: agent.name || node.id,
    });
    setTooltipPos({ x: event.clientX, y: event.clientY });
  }, [agents]);

  const onNodeMouseMove = useCallback((event) => {
    setTooltipPos((prev) => prev ? { x: event.clientX, y: event.clientY } : null);
  }, []);

  const onNodeMouseLeave = useCallback(() => {
    setHoveredAgent(null);
    setTooltipPos(null);
  }, []);

  // ── Empty state ──────────────────────────────────────────────────────────
  if (ids.length === 0) {
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
      {/* Status bar */}
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

      {/* ReactFlow canvas */}
      <div className="agent-tree-rf-wrapper">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          onNodeMouseEnter={onNodeMouseEnter}
          onNodeMouseMove={onNodeMouseMove}
          onNodeMouseLeave={onNodeMouseLeave}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          minZoom={0.2}
          maxZoom={2.5}
          defaultEdgeOptions={{
            style: { stroke: 'var(--border)', strokeWidth: 1 },
          }}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="var(--border)" gap={24} size={0.4} />
          <Controls
            className="rf-controls"
            showInteractive={false}
          />
          <MiniMap
            className="rf-minimap"
            nodeStrokeColor="var(--border)"
            nodeColor="#313244"
            maskColor="rgba(0,0,0,0.4)"
            pannable
            zoomable
          />
        </ReactFlow>
      </div>

      {/* Tooltip portal */}
      {hoveredAgent && tooltipPos && (
        <Tooltip agent={hoveredAgent} position={tooltipPos} />
      )}
    </div>
  );
}
