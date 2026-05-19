/**
 * Input - prompt + line editor backed by ink-text-input.
 *
 * Supports history navigation with up/down (when buffer is empty), and
 * intercepts Ctrl-C to forward as a cancel signal rather than killing
 * the process.
 *
 * When stdin doesn't support raw mode (e.g. piped/non-TTY), we render a
 * read-only placeholder so the rest of the UI can still display events.
 */
import React, {useState, useRef} from 'react';
import {Box, Text, useInput, useStdin} from 'ink';
import TextInput from 'ink-text-input';
import type {Theme} from '../themes.js';

export interface InputProps {
  theme:      Theme;
  disabled?:  boolean;
  onSubmit:   (text: string) => void;
  onCancel?:  () => void;
  prompt?:    string;
}

export const Input: React.FC<InputProps> = ({theme, disabled, onSubmit, onCancel, prompt = '>'}) => {
  const [value, setValue] = useState('');
  const history = useRef<string[]>([]);
  const cursor = useRef<number>(-1);
  const {isRawModeSupported} = useStdin();

  useInput((char, key) => {
    if (disabled) return;
    if (key.ctrl && char === 'c') {
      onCancel?.();
      return;
    }
    if (key.upArrow && value === '' && history.current.length > 0) {
      const idx = cursor.current === -1
        ? history.current.length - 1
        : Math.max(0, cursor.current - 1);
      cursor.current = idx;
      setValue(history.current[idx] ?? '');
    }
    if (key.downArrow && cursor.current !== -1) {
      const idx = cursor.current + 1;
      if (idx >= history.current.length) {
        cursor.current = -1;
        setValue('');
      } else {
        cursor.current = idx;
        setValue(history.current[idx] ?? '');
      }
    }
  }, {isActive: isRawModeSupported});

  const submit = (text: string) => {
    if (disabled) return;
    const trimmed = text.trim();
    if (!trimmed) return;
    history.current.push(trimmed);
    cursor.current = -1;
    setValue('');
    onSubmit(trimmed);
  };

  if (!isRawModeSupported) {
    return (
      <Box>
        <Text color={theme.dim} italic>(non-interactive stdin - input disabled)</Text>
      </Box>
    );
  }

  return (
    <Box>
      <Text color={theme.accent}>{prompt} </Text>
      {disabled
        ? <Text color={theme.dim} italic>(working - press Ctrl-C to cancel)</Text>
        : <TextInput value={value} onChange={setValue} onSubmit={submit} />}
    </Box>
  );
};
