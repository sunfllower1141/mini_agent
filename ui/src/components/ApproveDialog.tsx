/**
 * ApproveDialog - modal-like row that appears when the backend asks the
 * user to approve a write/destructive tool call.  Listens for y/N keys.
 */
import React from 'react';
import {Box, Text, useInput} from 'ink';
import type {ApprovalReq} from '../state.js';
import type {Theme} from '../themes.js';

export interface ApproveDialogProps {
  req:    ApprovalReq;
  theme:  Theme;
  onAnswer: (allow: boolean) => void;
}

export const ApproveDialog: React.FC<ApproveDialogProps> = ({req, theme, onAnswer}) => {
  useInput((input, key) => {
    if (input === 'y' || input === 'Y') onAnswer(true);
    else if (input === 'n' || input === 'N' || key.escape || key.return) onAnswer(false);
  });

  return (
    <Box borderStyle="double" borderColor={theme.pulse} paddingX={1} flexDirection="column">
      <Text color={theme.pulse} bold>approval requested</Text>
      <Text color={theme.text}>{req.toolName}({req.argsBrief})</Text>
      <Text color={theme.dim}>press [y] to allow, [n]/Esc/Enter to deny</Text>
    </Box>
  );
};
