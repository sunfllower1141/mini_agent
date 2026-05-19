/**
 * ChatPane - rolling assistant + user message log with live token streaming.
 *
 * Thinking blocks are rendered dim above the assistant content.
 * The live assistant is truncated to the last N lines to prevent
 * it from pushing tools/header off screen during long responses.
 */
import React from 'react';
import {Box, Text} from 'ink';
import type {ChatMsg} from '../state.js';
import type {Theme} from '../themes.js';

export interface ChatPaneProps {
  chat:          ChatMsg[];
  liveAssistant: ChatMsg | null;
  theme:         Theme;
  max?:          number;
}

/** Return last *n* lines of a string, joining with newline. */
function tail(s: string, n: number): string {
  if (!s) return s;
  const lines = s.split('\n');
  if (lines.length <= n) return s;
  return lines.slice(-n).join('\n');
}

const MessageView: React.FC<{msg: ChatMsg; theme: Theme; truncate?: boolean}> = ({msg, theme, truncate}) => {
  const thinking = truncate ? tail(msg.thinking ?? '', 6) : msg.thinking;
  const text = truncate ? tail(msg.text, 8) : msg.text;

  if (msg.role === 'user') {
    return (
      <Box flexDirection="column" marginBottom={1}>
        <Text color={theme.purple} bold>you</Text>
        <Text color={theme.text}>{text}</Text>
      </Box>
    );
  }
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Text color={theme.accent} bold>assistant</Text>
      {thinking && (
        <Box flexDirection="column" paddingX={1}>
          <Text color={theme.thinking} italic>{thinking}</Text>
          {truncate && (msg.thinking?.split('\n').length ?? 0) > 6 && (
            <Text color={theme.dim} italic>  ... (scrolled)</Text>
          )}
        </Box>
      )}
      {text && <Text color={theme.text}>{text}</Text>}
      {truncate && msg.text.split('\n').length > 8 && (
        <Text color={theme.dim} italic>  ... (scrolled)</Text>
      )}
    </Box>
  );
};

export const ChatPane: React.FC<ChatPaneProps> = ({chat, liveAssistant, theme, max = 30}) => {
  const visible = chat.slice(-max);
  return (
    <Box flexDirection="column" paddingX={1}>
      {visible.map((m) => <MessageView key={m.id} msg={m} theme={theme} />)}
      {liveAssistant && <MessageView msg={liveAssistant} theme={theme} truncate />}
    </Box>
  );
};
