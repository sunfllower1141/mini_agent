/**
 * SubAgentPanes - horizontal strip of live sub-agent output panes.
 * Plus a tree view summarising parent/child relationships.
 */
import React from 'react';
import {Box, Text} from 'ink';
import Spinner from 'ink-spinner';
import type {SubAgent} from '../state.js';
import type {Theme} from '../themes.js';

const TAIL_LINES = 6;

function tail(text: string, n: number): string[] {
  const lines = text.split('\n');
  return lines.slice(-n);
}

const SubAgentPane: React.FC<{a: SubAgent; theme: Theme}> = ({a, theme}) => {
  const lines = tail(a.buffer, TAIL_LINES);
  let badge: React.ReactNode;
  let color = theme.yellow;
  if (a.status === 'running') { badge = <Spinner type="dots" />; color = theme.yellow; }
  else if (a.status === 'done') { badge = <Text color={theme.green}>OK</Text>; color = theme.green; }
  else { badge = <Text color={theme.red}>X</Text>; color = theme.red; }

  return (
    <Box flexDirection="column" borderStyle="single" borderColor={theme.border}
         paddingX={1} marginRight={1} width={30}>
      <Box>
        <Box width={4}>{badge}</Box>
        <Text color={color} bold wrap="truncate-end">{a.taskId}</Text>
      </Box>
      {a.summary && <Text color={theme.dim} wrap="truncate-end">{a.summary}</Text>}
      <Box flexDirection="column" marginTop={1}>
        {lines.map((l, i) => (
          <Text key={i} color={theme.text} wrap="truncate-end">{l}</Text>
        ))}
      </Box>
    </Box>
  );
};

export interface SubAgentPanesProps {
  agents: Record<string, SubAgent>;
  theme:  Theme;
}

export const SubAgentPanes: React.FC<SubAgentPanesProps> = ({agents, theme}) => {
  const arr = Object.values(agents);
  if (arr.length === 0) return null;
  return (
    <Box>
      {arr.slice(0, 4).map((a) => <SubAgentPane key={a.taskId} a={a} theme={theme} />)}
      {arr.length > 4 && (
        <Box paddingX={1}>
          <Text color={theme.dim}>+{arr.length - 4} more</Text>
        </Box>
      )}
    </Box>
  );
};

// ----- AgentTree -----

export interface AgentTreeProps {
  agents: Record<string, SubAgent>;
  theme:  Theme;
}

export const AgentTree: React.FC<AgentTreeProps> = ({agents, theme}) => {
  const arr = Object.values(agents);
  if (arr.length === 0) {
    return (
      <Box flexDirection="column" borderStyle="round" borderColor={theme.border} paddingX={1}>
        <Text color={theme.accent} bold>agents</Text>
        <Text color={theme.dim} italic>orchestrator</Text>
      </Box>
    );
  }
  const byParent = new Map<string, SubAgent[]>();
  for (const a of arr) {
    const key = a.parent ?? '__root__';
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key)!.push(a);
  }
  const renderNode = (a: SubAgent, depth: number): React.ReactNode => {
    let glyph = theme.yellow;
    if (a.status === 'done') glyph = theme.green;
    if (a.status === 'error') glyph = theme.red;
    const kids = byParent.get(a.taskId) ?? [];
    return (
      <Box key={a.taskId} flexDirection="column">
        <Text>
          <Text color={theme.dim}>{'  '.repeat(depth)}{'- '}</Text>
          <Text color={glyph}>{a.status === 'running' ? '*' : a.status === 'done' ? 'v' : 'x'} </Text>
          <Text color={theme.text}>{a.taskId}</Text>
        </Text>
        {kids.map((k) => renderNode(k, depth + 1))}
      </Box>
    );
  };
  const roots = byParent.get('__root__') ?? [];
  return (
    <Box flexDirection="column" borderStyle="round" borderColor={theme.border} paddingX={1}>
      <Text color={theme.accent} bold>agents</Text>
      <Text color={theme.dim}>orchestrator</Text>
      {roots.map((r) => renderNode(r, 1))}
    </Box>
  );
};
