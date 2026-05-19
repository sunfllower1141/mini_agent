import React, { useRef, useEffect, useState } from 'react';
import { Graph } from '@cosmos.gl/graph';

/**
 * GraphView — GPU force graph of the workspace file tree.
 * Uses @cosmos.gl/graph (WebGL). Falls back to text tree on error.
 */
export default function GraphView({ nodes, edges, agentPositions, onNodeClick }) {
  const containerRef = useRef(null);
  const graphRef = useRef(null);
  const nodeMapRef = useRef({});
  const idxMapRef = useRef({});
  const [fallback, setFallback] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !nodes || nodes.length === 0) return;

    // Clean up
    if (graphRef.current) {
      graphRef.current = null;
    }
    container.innerHTML = '';

    const n = nodes.length;
    const nodeMap = {};
    const idxMap = {};

    nodes.forEach((node, i) => {
      nodeMap[i] = node;
      idxMap[node.id] = i;
    });

    // Positions — circle layout
    const positions = new Float32Array(n * 2);
    const radius = Math.min(400, n * 12);
    for (let i = 0; i < n; i++) {
      const angle = (2 * Math.PI * i) / n;
      positions[i * 2] = radius * Math.cos(angle);
      positions[i * 2 + 1] = radius * Math.sin(angle);
    }

    // Colors: 0-255 RGB, 0-1 alpha
    const colors = new Float32Array(n * 4);
    for (let i = 0; i < n; i++) {
      if (nodes[i].type === 'directory') {
        colors[i * 4] = 88; colors[i * 4 + 1] = 166; colors[i * 4 + 2] = 255; colors[i * 4 + 3] = 1.0;
      } else {
        colors[i * 4] = 140; colors[i * 4 + 1] = 148; colors[i * 4 + 2] = 158; colors[i * 4 + 3] = 0.8;
      }
    }

    // Sizes
    const sizes = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      sizes[i] = nodes[i].type === 'directory' ? 8.0 : 4.0;
    }

    // Links
    const edgeItems = [];
    edges.forEach((e) => {
      const src = idxMap[e.source];
      const tgt = idxMap[e.target];
      if (src !== undefined && tgt !== undefined) edgeItems.push(src, tgt);
    });
    const links = new Float32Array(edgeItems);

    // Link colors
    const linkCount = edgeItems.length / 2;
    const linkColors = new Float32Array(linkCount * 4);
    for (let i = 0; i < linkCount; i++) {
      linkColors[i * 4] = 48; linkColors[i * 4 + 1] = 54; linkColors[i * 4 + 2] = 61; linkColors[i * 4 + 3] = 0.5;
    }

    try {
      const graph = new Graph(container, {
        backgroundColor: '#0d1117',
        spaceSize: 8192,
        pointSizeScale: 1.0,
        linkWidthScale: 0.5,
        renderLinks: true,
        curvedLinks: true,
        curvedLinkSegments: 16,
        curvedLinkWeight: 0.3,
        simulationDecay: 3000,
        simulationGravity: 0.12,
        simulationRepulsion: 0.6,
        simulationLinkSpring: 0.3,
        simulationLinkDistance: 30,
        simulationFriction: 0.85,
        enableDrag: true,
        fitViewOnInit: true,
        fitViewDelay: 500,
        fitViewPadding: 0.15,
        onClick: (index) => {
          if (index !== undefined && nodeMap[index]) {
            onNodeClick(nodeMap[index].id);
          }
        },
      });

      graph.setPointPositions(positions);
      graph.setPointColors(colors);
      graph.setPointSizes(sizes);
      if (links.length > 0) {
        graph.setLinks(links);
        graph.setLinkColors(linkColors);
      }

      // CRITICAL: must call render
      graph.render();

      graphRef.current = graph;
      nodeMapRef.current = nodeMap;
      idxMapRef.current = idxMap;
      setFallback(false);
    } catch (err) {
      console.error('[GraphView] WebGL failed, falling back to text tree:', err);
      setFallback(true);
    }
  }, [nodes, edges]);

  // Update colors when agent positions change
  useEffect(() => {
    const graph = graphRef.current;
    const idxMap = idxMapRef.current;
    if (!graph || !nodes || nodes.length === 0) return;

    const agentIds = Object.keys(agentPositions);
    const colors = new Float32Array(nodes.length * 4);

    // Default: all dim
    for (let i = 0; i < nodes.length; i++) {
      colors[i * 4] = 88; colors[i * 4 + 1] = 166; colors[i * 4 + 2] = 255; colors[i * 4 + 3] = 0.3;
    }

    if (agentIds.length > 0) {
      const agentColors = [[255, 136, 62], [63, 185, 80], [188, 140, 255]];
      agentIds.forEach((agentId, ai) => {
        const idx = idxMap[agentPositions[agentId]];
        if (idx !== undefined) {
          const [r, g, b] = agentColors[ai % agentColors.length];
          colors[idx * 4] = r; colors[idx * 4 + 1] = g; colors[idx * 4 + 2] = b; colors[idx * 4 + 3] = 1.0;
        }
      });
    }

    try { graph.setPointColors(colors); } catch (e) { /* ignore */ }
  }, [agentPositions, nodes]);

  if (!nodes || nodes.length === 0) {
    return (
      <div className="graph-fallback">
        <div className="icon">🔌</div>
        <div className="title">Waiting for agent...</div>
        <div className="subtitle">Start mini_agent and the workspace graph will appear here.</div>
      </div>
    );
  }

  if (fallback) {
    return <TextTree nodes={nodes} edges={edges} agentPositions={agentPositions} onNodeClick={onNodeClick} />;
  }

  return <div ref={containerRef} style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 }} />;
}

function TextTree({ nodes, edges, agentPositions, onNodeClick }) {
  const childrenMap = {};
  edges.forEach((e) => { if (!childrenMap[e.source]) childrenMap[e.source] = []; childrenMap[e.source].push(e.target); });
  const nodeMap = {};
  nodes.forEach((n) => { nodeMap[n.id] = n; });
  const root = nodes.find((n) => n.type === 'directory' && !edges.some((e) => e.target === n.id));

  const render = (id, d) => {
    const node = nodeMap[id]; if (!node) return null;
    const active = Object.values(agentPositions).includes(id);
    return (
      <div key={id}>
        <div style={{ paddingLeft: d * 14 + 4, cursor: 'pointer', fontSize: 11, display: 'flex', alignItems: 'center', gap: 4 }}
             onClick={() => onNodeClick(id)} title={id}>
          {active && <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#58a6ff', flexShrink: 0 }} />}
          <span style={{ color: node.type === 'directory' ? '#58a6ff' : '#8b949e' }}>
            {node.type === 'directory' ? '📁' : '📄'} {node.label}
          </span>
        </div>
        {(childrenMap[id] || []).map((c) => render(c, d + 1))}
      </div>
    );
  };

  return (
    <div style={{ overflowY: 'auto', height: '100%', padding: 4, fontFamily: 'JetBrains Mono, monospace' }}>
      {root ? render(root.id, 0) : nodes.map((n) => (
        <div key={n.id} style={{ fontSize: 11, padding: '1px 4px', cursor: 'pointer' }} onClick={() => onNodeClick(n.id)}>
          {n.type === 'directory' ? '📁' : '📄'} {n.label}
        </div>
      ))}
    </div>
  );
}
