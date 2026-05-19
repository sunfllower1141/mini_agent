/**
 * App - root layout for the Ink CLI.
 *
 * Layout (top to bottom):
 *   1. Header (banner once ready)
 *   2. Two-column row: ToolsLog (left, flex=2) | AgentTree (right, flex=1)
 *   3. SubAgentPanes (when any active)
 *   4. ChatPane
 *   5. ApproveDialog (when pending)
 *   6. Input row
 *   7. StatusBar
 */
import React, {useEffect, useReducer, useState} from 'react';
import {Box, Text, useApp, useInput, useStdin} from 'ink';

import {IpcClient, EVT, CMD} from '../ipc.js';
import {reducer, initialState} from '../state.js';
import {resolveTheme, THEME_NAMES, DEFAULT_THEME} from '../themes.js';

import {ChatPane} from './ChatPane.js';
import {ToolCard} from './ToolCard.js';
import {AgentTree, SubAgentPanes} from './SubAgentPanes.js';
import {StatusBar} from './StatusBar.js';
import {Input} from './Input.js';
import {ApproveDialog} from './ApproveDialog.js';

export interface AppProps {
  ipc:           IpcClient;
  initialTheme?: string;
}

const SLASH_COMMANDS = new Set([
  'init', 'clear', 'export', 'stats', 'session', 'workspace', 'help', 'theme', 'quit',
]);

function parseSlash(text: string): {name: string; args: Record<string, any>} | null {
  if (!text.startsWith('/')) return null;
  const stripped = text.slice(1).trim();
  if (!stripped) return null;
  const parts = stripped.split(/\s+/);
  const name = (parts[0] ?? '').toLowerCase();
  if (!SLASH_COMMANDS.has(name)) return null;
  const rest = parts.slice(1);
  if (name === 'session') {
    return {name, args: {sub: rest[0] ?? '', name: rest[1] ?? ''}};
  }
  if (name === 'workspace') {
    return {name, args: {path: rest.join(' ')}};
  }
  if (name === 'theme') {
    return {name, args: {theme: rest[0] ?? ''}};
  }
  return {name, args: {}};
}

export const App: React.FC<AppProps> = ({ipc, initialTheme}) => {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [themeName, setThemeName] = useState<string>(initialTheme ?? DEFAULT_THEME);
  const theme = resolveTheme(themeName);
  const {exit} = useApp();
  const {isRawModeSupported} = useStdin();

  useEffect(() => {
    const off = ipc.onEvent((evt) => dispatch({type: 'IPC_EVENT', evt}));
    return () => {
      off();
    };
  }, [ipc]);

  useInput((input, key) => {
    if (key.ctrl && input === 'q') {
      ipc.shutdown();
      setTimeout(() => exit(), 300);
    }
  }, {isActive: isRawModeSupported});

  const handleSubmit = (text: string) => {
    // /quit handled locally
    if (text === '/quit' || text === '/exit') {
      ipc.shutdown();
      setTimeout(() => exit(), 300);
      return;
    }
    // /help handled locally - just append to logs.
    if (text === '/help' || text === '/h') {
      const help = [
        '/init - reinitialize .mini_agent.rules and .mini_agent.toml',
        '/clear - reset conversation memory',
        '/export - export conversation to markdown',
        '/stats - session statistics',
        '/session list|new <n>|switch <n>|delete <n>',
        '/workspace <path>',
        `/theme <name> - one of ${THEME_NAMES.join(', ')}`,
        '/quit, /exit - exit the CLI',
      ].join('\n');
      dispatch({type: 'IPC_EVENT', evt: {type: EVT.LOG, data: {level: 'info', msg: help}, ts: Date.now() / 1000}});
      return;
    }
    // /theme handled locally - no backend round-trip needed.
    const slash = parseSlash(text);
    if (slash && slash.name === 'theme') {
      const target = String(slash.args.theme ?? '').toLowerCase();
      if (THEME_NAMES.includes(target)) {
        setThemeName(target);
      } else {
        dispatch({type: 'IPC_EVENT', evt: {type: EVT.LOG, data: {level: 'warn', msg: `unknown theme: ${target}`}, ts: Date.now() / 1000}});
      }
      return;
    }
    if (slash) {
      ipc.send(CMD.USER_COMMAND, {name: slash.name, args: slash.args});
      return;
    }
    // Plain message
    dispatch({type: 'SUBMIT_USER', text});
    ipc.send(CMD.USER_MESSAGE, {text});
  };

  const handleApprove = (allow: boolean) => {
    if (!state.pendingApproval) return;
    ipc.send(CMD.USER_APPROVE, {id: state.pendingApproval.id, allow});
    dispatch({type: 'CLEAR_APPROVAL'});
  };

  const handleCancel = () => {
    if (state.turnInFlight) ipc.send(CMD.USER_CANCEL);
  };

  const banner = state.banner;
  const recentLog = state.logs[state.logs.length - 1];
  const recentTools = state.tools.slice(-8);

  return (
    <Box flexDirection="column">
      {/* Header */}
      <Box paddingX={1}>
        <Text backgroundColor={theme.surface} color={theme.accent} bold>mini_agent</Text>
        {banner && (
          <Text backgroundColor={theme.surface} color={theme.dim}>  {banner.model}  -  {banner.workspace}
            {banner.restored > 0 ? `  (restored ${banner.restored} msgs)` : ''}</Text>
        )}
      </Box>

      {/* Tools — compact, always visible */}
      <Box flexDirection="column" paddingX={1}>
        <Box borderStyle="single" borderColor={theme.border} paddingX={1} marginBottom={1}>
          <Text color={theme.accent} bold>tools</Text>
          {recentTools.length > 0 && (
            <Text color={theme.dim}>  ({recentTools.length} calls)</Text>
          )}
          {recentTools.length === 0 && (
            <Text color={theme.dim} italic>  no tool calls yet</Text>
          )}
        </Box>
        {recentTools.map((t) => (
          <ToolCard key={t.seq} tool={t} theme={theme} />
        ))}
      </Box>

      {/* Agents tree (compact) */}
      <AgentTree agents={state.subagents} theme={theme} />

      <SubAgentPanes agents={state.subagents} theme={theme} />

      {/* Chat — recent messages only so tools stay visible */}
      <ChatPane chat={state.chat} liveAssistant={state.liveAssistant} theme={theme} max={8} />

      {state.errors.slice(-3).map((e, i) => (
        <Box key={i} paddingX={1}>
          <Text color={theme.red}>error: {e}</Text>
        </Box>
      ))}

      {recentLog && (
        <Box paddingX={1}>
          <Text color={recentLog.level === 'warn' || recentLog.level === 'stderr' ? theme.yellow : theme.dim}
                wrap="wrap">
            {recentLog.msg}
          </Text>
        </Box>
      )}

      {state.pendingApproval && (
        <ApproveDialog req={state.pendingApproval} theme={theme} onAnswer={handleApprove} />
      )}

      <Input theme={theme} disabled={state.turnInFlight} onSubmit={handleSubmit} onCancel={handleCancel} />

      <StatusBar status={state.status} theme={theme} turnInFlight={state.turnInFlight} themeName={themeName} />
    </Box>
  );
};
