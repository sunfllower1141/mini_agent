/**
 * ToolCard - one tool invocation row.
 *
 * Renders: status glyph + tool summary, optional tail of streamed output,
 * and a syntax-highlighted diff preview if the tool returned one.
 */
import React from 'react';
import {Box, Text} from 'ink';
import Spinner from 'ink-spinner';
import type {ToolCallState} from '../state.js';
import type {Theme} from '../themes.js';
import {CodeBlock} from './CodeBlock.js';

export interface ToolCardProps {
  tool:  ToolCallState;
  theme: Theme;
}

export const ToolCard: React.FC<ToolCardProps> = ({tool, theme}) => {
  let glyph: React.ReactNode;
  let color = theme.yellow;
  if (tool.status === 'running') {
    glyph = <Spinner type="dots" />;
    color = theme.yellow;
  } else if (tool.status === 'ok') {
    glyph = <Text color={theme.green}>OK</Text>;
    color = theme.green;
  } else {
    glyph = <Text color={theme.red}>FAIL</Text>;
    color = theme.red;
  }

  const tail = tool.outputLines.slice(-6);

  return (
    <Box flexDirection="column" marginBottom={1}>
      <Box>
        <Box width={6}>{glyph}</Box>
        <Text color={color} bold>{tool.summary}</Text>
        {tool.parallel && <Text color={theme.dim}> (parallel)</Text>}
      </Box>
      {tool.status !== 'running' && tool.detail && (
        <Box paddingLeft={6}>
          <Text color={theme.dim}>{tool.detail}</Text>
        </Box>
      )}
      {tail.length > 0 && (
        <Box flexDirection="column" paddingLeft={6}>
          {tail.map((line, i) => (
            <Text key={i} color={theme.dim} wrap="truncate-end">{line}</Text>
          ))}
        </Box>
      )}
      {tool.diffPreview && (
        <Box paddingLeft={6} marginTop={0}>
          <CodeBlock code={tool.diffPreview} language="diff" theme={theme} maxLines={20} />
        </Box>
      )}
    </Box>
  );
};
