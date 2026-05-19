/**
 * ToolsLog - tool call history.
 *
 * Renders completed tools first (they persist via Ink's <Static> in App.tsx),
 * then running tools with spinner below.
 */
import React from 'react';
import {Box, Text} from 'ink';
import type {ToolCallState} from '../state.js';
import type {Theme} from '../themes.js';
import {ToolCard} from './ToolCard.js';

export interface ToolsLogProps {
  tools: ToolCallState[];
  theme: Theme;
}

export const ToolsLog: React.FC<ToolsLogProps> = ({tools, theme}) => {
  const completed = tools.filter((t) => t.status !== 'running');
  const running = tools.filter((t) => t.status === 'running');
  return (
    <Box flexDirection="column" paddingX={1}>
      {/* Running tools animate with spinner */}
      {running.length > 0 && (
        <Box flexDirection="column" marginBottom={1}>
          {running.map((t) => (
            <ToolCard key={t.seq} tool={t} theme={theme} />
          ))}
        </Box>
      )}
      {/* Header line */}
      <Box borderStyle="single" borderColor={theme.border} paddingX={1} marginBottom={1}>
        <Text color={theme.accent} bold>tools</Text>
        <Text color={theme.dim}> </Text>
        <Text color={theme.dim}>({completed.length + running.length} calls{running.length > 0 ? `, ${running.length} running` : ''})</Text>
        {tools.length === 0 && <Text color={theme.dim} italic>  no tool calls yet</Text>}
      </Box>
    </Box>
  );
};
