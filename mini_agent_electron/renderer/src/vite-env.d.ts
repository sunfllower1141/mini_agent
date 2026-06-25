/// <reference types="vite/client" />

interface MiniAgentAPI {
  submit: (text: string) => Promise<void>;
  command: (cmd: string) => Promise<void>;
  cancel: () => Promise<void>;
  openWorkspace: () => Promise<string | null>;
  saveWorkspace: (path: string) => Promise<void>;
  newSession: (name: string) => Promise<void>;
  switchSession: (name: string) => Promise<void>;
  deleteSession: (name: string) => Promise<{ ok: boolean; message?: string }>;
  listSessions: () => Promise<{ names: string[] }>;
  getStatus: () => Promise<Record<string, unknown>>;
  setModel: (model: string) => Promise<void>;
  getTheme: () => Promise<string>;
  saveTheme: (themeId: string) => Promise<void>;
  restartBackend: () => Promise<void>;
  startBot: (script: string) => Promise<void>;
  stopBot: (script: string) => Promise<void>;
  getFilePath: (file: File) => string | null;
  onPaste: (callback: (records: FileRecord[]) => void) => () => void;
  on: (channel: string, callback: (data: any) => void) => () => void;
  removeAllListeners: (channel: string) => void;
}

interface FileRecord {
  name: string;
  type: string;
  size: number;
  path?: string;
  dataUrl?: string;
  _id?: string;
}

interface Window {
  miniAgent: MiniAgentAPI;
}

// Log line types used by App.tsx
interface LogLine {
  id?: number;
  _key?: string;
  text?: string;
  cls?: string;
  html?: string;
  icon?: string;
  component?: React.ReactNode;
  toolName?: string;
  toolArgs?: string;
  markdown?: boolean;
  thinkingText?: string;
  thinkingActive?: boolean;
  promptText?: string;
}

interface ToolLine {
  _key?: string;
  cls?: string;
  text?: string;
  promptText?: string;
  thinkingText?: string;
  thinkingActive?: boolean;
}

interface SubAgentData {
  name: string;
  desc: string;
  parent_id: string;
  toolCalls: Array<{
    toolName: string;
    toolArgs: string;
    ok: boolean | null;
    result?: string;
  }>;
  thoughts: string[];
  output: string;
  ok: boolean | null;
}

interface ThemeEntry {
  id: string;
  name: string;
  icon: string;
}

interface ModelGroup {
  group: string;
  models: Array<{
    id: string;
    label: string;
  }>;
}
