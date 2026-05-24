import { useEffect, useState, useRef, useCallback } from 'react';
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  useNodesState,
  useEdgesState,
  useReactFlow,
  Handle,
  Position,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import ELK from 'elkjs/lib/elk.bundled.js';

const elk = new ELK();

/**
 * AgentTree — hierarchical agent visualization using React Flow + elkjs.
 *
 * Uses the FLAT graph pattern from the official React Flow elkjs example:
 *   https://reactflow.dev/examples/layout/elkjs
 *
 * Converts our flat agent map into a flat list of ELK nodes + ELK edges,
 * runs elk.layout(), then maps the result to ReactFlow nodes/edges.
 *
 * Props:
 *   agents: object { [task_id]: { name, desc, parent_id, toolCalls, thoughts, output, ok } }
 */

// -------------------------------- ELK layout options --------------------------------

const ELK_OPTIONS = {
  'elk.algorithm': 'layered',
  'elk.direction': 'DOWN',
  'elk.spacing.nodeNode': '40',
  'elk.layered.spacing.nodeNodeBetweenLayers': '60',
  'elk.edgeRouting': 'ORTHOGONAL',
  'elk.padding': '[top=30,left=30,bottom=30,right=30]',
};

// -------------------------------- Agent node card --------------------------------

function AgentNode({ data }) {
  const { agent, isOrchestrator } = data;

  if (isOrchestrator) {
    return (
      <div className="agent-rf-node agent-rf-node--orch">
        🧠 Orchestrator
        <Handle type="source" position={Position.Bottom} id="source" />
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
      <Handle type="target" position={Position.Top} id="target" />
      <span className="agent-rf-node__dot" style={{ background: statusColor }} />
      <span className="agent-rf-node__name">{shortName}</span>
      {toolCount > 0 && (
        <span className="agent-rf-node__tools dim">{toolCount}t</span>
      )}
      <span className="agent-rf-node__status" style={{ color: statusColor }}>
        {statusIcon}
      </span>
      <Handle type="source" position={Position.Bottom} id="source" />
    </div>
  );
}

const nodeTypes = { agentNode: AgentNode };

// -------------------------------- Tooltip --------------------------------

function Tooltip({ agent, position }) {
  if (!agent || !position) return null;

  const { taskId, desc, toolCalls, thoughts, output, ok, name } = agent.attributes || agent;

  return (
    <div
      className="agent-tree-tooltip"
      style={{
        left: position.x + 12,
        top: position.y + 12,
        position: 'fixed',
        zIndex: 1000,
        maxWidth: 320,
      }}
    >
      <div className="agent-tree-tooltip-header">
        <span className="accent">{name || taskId}</span>
        <span className="dim" style={{ fontSize: 9, marginLeft: 6 }}>
          {taskId?.slice(0, 8)}
        </span>
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
              <span key={i} className="thinking" style={{ display: 'block', fontSize: 10, lineHeight: 1.3 }}>
                {t}
              </span>
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

// -------------------------------- Wrapper with provider --------------------------------

export default function AgentTree({ agents }) {
  return (
    <ReactFlowProvider>
      <AgentTreeInner agents={agents} />
    </ReactFlowProvider>
  );
}

// -------------------------------- Inner component --------------------------------

function AgentTreeInner({ agents }) {
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [hoveredAgent, setHoveredAgent] = useState(null);
  const [tooltipPos, setTooltipPos] = useState(null);
  const [debugInfo, setDebugInfo] = useState({
    agentCount: 0, nodeCount: 0, edgeCount: 0, error: '',
  });

  const { fitView } = useReactFlow();
  const prevCountRef = useRef(0);
  const ids = Object.keys(agents);

  // ── Build flat ELK graph and run layout ──────────────────────────
  useEffect(() => {
    if (ids.length === 0) {
      setNodes([]);
      setEdges([]);
      setDebugInfo({ agentCount: 0, nodeCount: 0, edgeCount: 0, error: 'empty' });
      return;
    }

    let cancelled = false;

    async function runLayout() {
      // 1. Build flat list of ELK nodes
      const allIds = ['orchestrator', ...ids];
      const elkChildren = allIds.map((id) => ({
        id,
        width: id === 'orchestrator' ? 170 : 160,
        height: id === 'orchestrator' ? 48 : 40,
        targetPosition: 'top',
        sourcePosition: 'bottom',
      }));

      // 2. Build flat list of ELK edges
      const elkEdges = ids.map((childId) => {
        const agent = agents[childId];
        const parentId = agent?.parent_id || 'orchestrator';
        return {
          id: `${parentId}->${childId}`,
          sources: [parentId],
          targets: [childId],
        };
      });

      const graph = {
        id: 'mini-agent-tree',
        layoutOptions: ELK_OPTIONS,
        children: elkChildren,
        edges: elkEdges,
      };

      console.log('[AgentTree] ELK input graph:', JSON.stringify(graph));

      try {
        const layoutedGraph = await elk.layout(graph);
        if (cancelled) return;
        console.log('[AgentTree] ELK output:', JSON.stringify(layoutedGraph));

        // 3. Map ELK nodes → ReactFlow nodes
        const layChildren = layoutedGraph.children || [];
        const newNodes = layChildren.map((ln) => ({
          id: ln.id,
          type: 'agentNode',
          position: { x: ln.x ?? 0, y: ln.y ?? 0 },
          sourcePosition: 'bottom',
          targetPosition: 'top',
          draggable: false,
          data: {
            agent: ln.id === 'orchestrator' ? null : agents[ln.id],
            isOrchestrator: ln.id === 'orchestrator',
          },
        }));

        // 4. Map ELK edges → ReactFlow edges
        const layEdges = layoutedGraph.edges || [];
        const newEdges = ids.map((childId) => {
          const agent = agents[childId];
          const parentId = agent?.parent_id || 'orchestrator';
          const isRunning = agent?.ok == null;
          return {
            id: `${parentId}->${childId}`,
            source: parentId,
            target: childId,
            type: 'default',
            animated: isRunning,
            markerEnd: {
              type: 'arrowclosed',
              width: 14,
              height: 14,
              color: isRunning ? '#999' : '#555',
            },
            style: {
              stroke: isRunning ? '#999' : '#555',
              strokeWidth: isRunning ? 2.5 : 1.5,
            },
          };
        });

        console.log('[AgentTree] newNodes:', newNodes.length, JSON.stringify(newNodes.map(n => ({ id: n.id, x: n.position.x, y: n.position.y }))));
        console.log('[AgentTree] newEdges:', newEdges.length, JSON.stringify(newEdges.map(e => ({ id: e.id, source: e.source, target: e.target }))));

        setNodes(newNodes);
        setEdges(newEdges);
        setDebugInfo({
          agentCount: ids.length,
          nodeCount: newNodes.length,
          edgeCount: newEdges.length,
          error: '',
        });

        // Auto-fit on first load or when agent count changes
        if (prevCountRef.current !== ids.length) {
          prevCountRef.current = ids.length;
          setTimeout(() => {
            fitView({ padding: 0.2, duration: 300 });
          }, 100);
        }
      } catch (err) {
        console.error('[AgentTree] ELK layout failed:', err);
        setDebugInfo({
          agentCount: ids.length, nodeCount: 0, edgeCount: 0,
          error: err?.message || String(err),
        });
      }
    }

    runLayout();
    return () => { cancelled = true; };
  }, [agents, setNodes, setEdges, fitView, ids.length]);

  // ── Tooltip handlers ────────────────────────────────────────────
  const handleNodeMouseEnter = useCallback(
    (event, node) => {
      if (node.data?.isOrchestrator) return;
      setHoveredAgent({ agent: node.data.agent, taskId: node.id });
      // Use viewport-relative mouse coordinates so the tooltip follows the cursor,
      // not React Flow's internal coordinate system (which shifts on pan/zoom).
      setTooltipPos({ x: event.clientX, y: event.clientY });
    },
    []
  );

  const handleNodeMouseMove = useCallback(
    (event, node) => {
      if (node.data?.isOrchestrator) return;
      setTooltipPos({ x: event.clientX, y: event.clientY });
    },
    []
  );

  const handleNodeMouseLeave = useCallback(() => {
    setHoveredAgent(null);
    setTooltipPos(null);
  }, []);

  // ── Render ──────────────────────────────────────────────────────
  if (ids.length === 0) {
    return (
      <div className="agent-tree-empty">
        No active sub-agents
      </div>
    );
  }

  return (
    <>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        fitView={false}
        onNodeMouseEnter={handleNodeMouseEnter}
        onNodeMouseMove={handleNodeMouseMove}
        onNodeMouseLeave={handleNodeMouseLeave}
        minZoom={0.1}
        maxZoom={2}
      >
        <Background />
      </ReactFlow>

      <Tooltip
        agent={
          hoveredAgent
            ? { attributes: { ...(hoveredAgent.agent || {}), taskId: hoveredAgent.taskId } }
            : null
        }
        position={tooltipPos}
      />

      {/* Debug overlay in dev */}
      {process.env.NODE_ENV !== 'production' && (
        <div style={{
          position: 'absolute', top: 4, right: 4, zIndex: 100,
          background: 'rgba(20,20,20,0.85)', color: '#aaa', fontSize: 9,
          fontFamily: 'monospace', padding: '2px 6px', borderRadius: 4,
        }}>
          agents:{debugInfo.agentCount} nodes:{debugInfo.nodeCount} edges:{debugInfo.edgeCount}
          {debugInfo.error ? ` ⚠️${debugInfo.error}` : ''}
        </div>
      )}
    </>
  );
}
