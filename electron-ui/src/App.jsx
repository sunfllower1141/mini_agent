import React, { useState, useEffect, useRef, useCallback } from 'react';
import GraphView from './components/GraphView';
import ActivityFeed from './components/ActivityFeed';
import StatusBar from './components/StatusBar';

const WS_URL = 'ws://127.0.0.1:8765';

export default function App() {
  const [ws, setWs] = useState(null);
  const [connected, setConnected] = useState(false);
  const [graphData, setGraphData] = useState({ nodes: [], edges: [] });
  const [activities, setActivities] = useState([]);
  const [streamText, setStreamText] = useState('');
  const [streamSource, setStreamSource] = useState('');
  const [agentPositions, setAgentPositions] = useState({});
  const reconnectTimer = useRef(null);
  const streamRef = useRef('');

  // Connect / reconnect WebSocket
  const connect = useCallback(() => {
    if (reconnectTimer.current) clearTimeout(reconnectTimer.current);

    const socket = new WebSocket(WS_URL);
    setWs(socket);

    socket.onopen = () => {
      setConnected(true);
      console.log('[ws] connected');
    };

    socket.onclose = () => {
      setConnected(false);
      setWs(null);
      console.log('[ws] disconnected — reconnecting in 2s');
      reconnectTimer.current = setTimeout(connect, 2000);
    };

    socket.onerror = (err) => {
      console.error('[ws] error', err);
      socket.close();
    };

    socket.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleMessage(msg);
      } catch (e) {
        console.warn('[ws] bad message', e);
      }
    };

    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    };
  }, [connect]);

  // Dispatch incoming messages
  const handleMessage = (msg) => {
    const { type, data, ts } = msg;

    switch (type) {
      case 'graph.init':
        setGraphData(data);
        break;

      case 'tool.start':
        setActivities((prev) => [
          ...prev.slice(-499),
          {
            id: `${ts}-${Math.random()}`,
            type: 'tool.start',
            toolName: data.name,
            filePath: data.file_path || '',
            agentId: data.agent_id || '',
            argsPreview: data.args_preview || '',
            ts,
          },
        ]);
        break;

      case 'tool.result':
        setActivities((prev) => [
          ...prev.slice(-499),
          {
            id: `${ts}-${Math.random()}`,
            type: 'tool.result',
            toolName: data.name,
            filePath: data.file_path || '',
            agentId: data.agent_id || '',
            success: data.success,
            summary: data.summary || '',
            ts,
          },
        ]);
        // Update agent position
        if (data.agent_id && data.file_path) {
          setAgentPositions((prev) => ({
            ...prev,
            [data.agent_id]: data.file_path,
          }));
        }
        break;

      case 'stream.token':
        if (data.agent_id !== streamRef.current) {
          streamRef.current = data.agent_id || '';
          setStreamText('');
          setStreamSource(data.agent_id || '');
        }
        setStreamText((prev) => prev + data.token);
        break;

      case 'agent.heartbeat':
        setActivities((prev) => [
          ...prev.slice(-499),
          {
            id: `${ts}-${Math.random()}`,
            type: 'heartbeat',
            agentId: data.agent_id || '',
            status: data.status || '',
            ts,
          },
        ]);
        break;

      default:
        break;
    }
  };

  // Send a chat message
  const sendMessage = (text) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({
      type: 'ui.send_message',
      data: { text },
    }));
  };

  // Request file inspection
  const inspectFile = (filePath) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({
      type: 'ui.click_node',
      data: { file_path: filePath },
    }));
  };

  return (
    <div className="app-container">
      {/* Left: Workspace Graph */}
      <div className="panel graph-panel">
        <div className="panel-header">
          <span>Workspace</span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
            {graphData.nodes.length} nodes
          </span>
        </div>
        <div className="graph-container">
          <GraphView
            nodes={graphData.nodes}
            edges={graphData.edges}
            agentPositions={agentPositions}
            onNodeClick={inspectFile}
          />
        </div>
      </div>

      {/* Right: Activity + Stream */}
      <div className="panel right-panel">
        <div className="panel-header">
          <span>Activity</span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
            {activities.length} events
          </span>
        </div>
        <ActivityFeed activities={activities} />

        {/* Stream output */}
        {streamText && (
          <div className="stream-panel">
            <div className="stream-panel-header">
              Streaming {streamSource ? `— ${streamSource}` : ''}
            </div>
            <div className="stream-panel-content">{streamText}</div>
          </div>
        )}

        {/* Chat */}
        <div className="chat-bar">
          <input
            type="text"
            placeholder="Send a message to the agent..."
            onKeyDown={(e) => {
              if (e.key === 'Enter' && e.target.value.trim()) {
                sendMessage(e.target.value.trim());
                e.target.value = '';
              }
            }}
          />
          <button
            onClick={(e) => {
              const input = e.target.parentElement.querySelector('input');
              if (input.value.trim()) {
                sendMessage(input.value.trim());
                input.value = '';
              }
            }}
          >
            Send
          </button>
        </div>
      </div>

      {/* Status Bar */}
      <StatusBar
        connected={connected}
        agentCount={Object.keys(agentPositions).length}
        activityCount={activities.length}
        nodeCount={graphData.nodes.length}
      />
    </div>
  );
}
