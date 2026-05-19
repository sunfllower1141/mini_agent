/**
 * StatusBar - single-line footer with model, workspace, git, and turn stats.
 */
import React from 'react';
import {Box, Text} from 'ink';
import Spinner from 'ink-spinner';
import type {Status} from '../state.js';
import type {Theme} from '../themes.js';

export interface StatusBarProps {
  status:       Status;
  theme:        Theme;
  turnInFlight: boolean;
  themeName:    string;
}

function shortPath(p?: string, n = 40): string {
  if (!p) return '';
  if (p.length <= n) return p;
  return '...' + p.slice(p.length - n + 3);
}

export const StatusBar: React.FC<StatusBarProps> = ({status, theme, turnInFlight, themeName}) => {
  const gitLabel = status.gitBranch
    ? `${status.gitBranch}${status.gitDirty ? '*' : ''}`
    : '';
  return (
    <Box paddingX={1}>
      <Box width={3}>
        {turnInFlight ? <Spinner type="dots" /> : <Text color={theme.dim} backgroundColor={theme.surface}>{'>>'}</Text>}
      </Box>
      <Text backgroundColor={theme.surface} color={theme.accent} bold>{status.model ?? ''}</Text>
      <Text backgroundColor={theme.surface} color={theme.dim}>  </Text>
      <Text backgroundColor={theme.surface} color={theme.dim}>{shortPath(status.workspace)}</Text>
      {gitLabel && (<>
        <Text backgroundColor={theme.surface} color={theme.dim}>  </Text>
        <Text backgroundColor={theme.surface} color={theme.green}>{gitLabel}</Text>
      </>)}
      <Text backgroundColor={theme.surface} color={theme.dim}>  turns:{status.totalTurns ?? 0} tools:{status.totalToolCalls ?? 0}  theme:{themeName}</Text>
    </Box>
  );
};
