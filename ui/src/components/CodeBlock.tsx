/**
 * CodeBlock - syntax-highlighted code via ink-syntax-highlight.
 *
 * Falls back to a plain Text node if the highlighter throws (unknown
 * language, malformed input, etc.).
 */
import React from 'react';
import {Text, Box} from 'ink';
import SyntaxHighlight from 'ink-syntax-highlight';
import type {Theme} from '../themes.js';

export interface CodeBlockProps {
  code:      string;
  language?: string;     // 'python', 'typescript', 'diff', 'bash', ...
  theme:     Theme;
  maxLines?: number;
}

function detectLanguage(code: string, hint?: string): string {
  if (hint) return hint;
  if (code.startsWith('---') || /^[-+]{1,3} /m.test(code)) return 'diff';
  if (/^\s*def |^\s*class .*:/m.test(code)) return 'python';
  if (/^\s*(import|export|const|function)\b/m.test(code)) return 'typescript';
  return 'plaintext';
}

export const CodeBlock: React.FC<CodeBlockProps> = ({code, language, theme, maxLines}) => {
  let displayed = code;
  let truncatedCount = 0;
  if (maxLines && maxLines > 0) {
    const lines = code.split('\n');
    if (lines.length > maxLines) {
      truncatedCount = lines.length - maxLines;
      displayed = lines.slice(0, maxLines).join('\n');
    }
  }

  const lang = detectLanguage(displayed, language);

  let body: React.ReactNode;
  try {
    body = <SyntaxHighlight code={displayed} language={lang} />;
  } catch {
    body = <Text color={theme.text}>{displayed}</Text>;
  }

  return (
    <Box flexDirection="column" borderStyle="single" borderColor={theme.border} paddingX={1}>
      {body}
      {truncatedCount > 0 && (
        <Text color={theme.dim} italic>... {truncatedCount} more line{truncatedCount === 1 ? '' : 's'}</Text>
      )}
    </Box>
  );
};
