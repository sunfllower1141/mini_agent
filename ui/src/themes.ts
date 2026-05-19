/**
 * themes.ts ÃÃÃÂ¢ ported from tui.py (Textual TUI).
 *
 * Ink uses chalk-style colour names rather than CSS hex.  We keep the hex
 * palettes verbatim from the Python side ÃÃÃÂ¢ Ink/chalk accepts hex strings via
 * the `color` and `backgroundColor` props on <Text> and <Box>.
 */

export interface Theme {
  name:     string;
  bg:       string;  // app background (panes)
  surface:  string;  // header, footer, input
  border:   string;  // separators
  accent:   string;  // header, highlights
  text:     string;  // primary
  dim:      string;  // secondary
  green:    string;
  yellow:   string;
  red:      string;
  thinking: string;  // dim grey for thought stream
  pulse:    string;  // attention / approval glow
  purple:   string;  // interjection queued
}

export const THEMES: Record<string, Theme> = {
  dawn: {
    name: 'Dawn',
    bg: '#faf8f5', surface: '#f0ede8', border: '#d4cfc8',
    accent: '#b8956a', text: '#3d3a35', dim: '#8a857d',
    green: '#5a8a4a', yellow: '#b89540', red: '#c06050',
    thinking: '#b0aaa0', pulse: '#f0c060', purple: '#a080c0',
  },
  sepia: {
    name: 'Sepia',
    bg: '#f4f0e6', surface: '#e8e0d0', border: '#c8b898',
    accent: '#b8893a', text: '#4a3f30', dim: '#8a7a60',
    green: '#6a8a4a', yellow: '#c0a040', red: '#b85840',
    thinking: '#b0a080', pulse: '#e0b040', purple: '#9a7ab0',
  },
  ember: {
    name: 'Ember',
    bg: '#1e1814', surface: '#2a221c', border: '#3a3028',
    accent: '#d4985a', text: '#d0c8be', dim: '#7a7064',
    green: '#7ab860', yellow: '#d4a040', red: '#d47050',
    thinking: '#5a5040', pulse: '#e89840', purple: '#c090d0',
  },
  slate: {
    name: 'Slate',
    bg: '#111111', surface: '#1b1b1b', border: '#2a2a2a',
    accent: '#8f8f8f', text: '#b8b8b8', dim: '#5a5a5a',
    green: '#4f9f6f', yellow: '#b89a4a', red: '#a85a5a',
    thinking: '#3a3a3a', pulse: '#c0c040', purple: '#8a7ab0',
  },
  midnight: {
    name: 'Midnight',
    bg: '#090b0d', surface: '#131619', border: '#1e2226',
    accent: '#8899aa', text: '#b0c0d0', dim: '#4a5560',
    green: '#4a8a6a', yellow: '#9a8a4a', red: '#9a6060',
    thinking: '#2a3040', pulse: '#6a8acc', purple: '#7a8ab0',
  },
  cobalt: {
    name: 'Cobalt',
    bg: '#0a1220', surface: '#101830', border: '#1e2850',
    accent: '#6090d0', text: '#a0b8d8', dim: '#4a6090',
    green: '#5a9a6a', yellow: '#a0a040', red: '#b06060',
    thinking: '#203050', pulse: '#5090e0', purple: '#8090d0',
  },
  neon: {
    name: 'Neon',
    bg: '#0c0c0c', surface: '#16161a', border: '#303030',
    accent: '#e040e0', text: '#c0e0c0', dim: '#506050',
    green: '#00e060', yellow: '#e0c000', red: '#ff4060',
    thinking: '#302040', pulse: '#e040ff', purple: '#c040ff',
  },
  forest: {
    name: 'Forest',
    bg: '#0e1410', surface: '#141c16', border: '#1e2e22',
    accent: '#60a870', text: '#a0c0a8', dim: '#4a6a50',
    green: '#60d070', yellow: '#b0b040', red: '#c06050',
    thinking: '#203028', pulse: '#50d060', purple: '#8090b0',
  },
  dracula: {
    name: 'Dracula',
    bg: '#282a36', surface: '#1e1f29', border: '#44475a',
    accent: '#bd93f9', text: '#f8f8f2', dim: '#6272a4',
    green: '#50fa7b', yellow: '#f1fa8c', red: '#ff5555',
    thinking: '#44475a', pulse: '#ff79c6', purple: '#bd93f9',
  },
};

export const DEFAULT_THEME = 'slate';

export function resolveTheme(name?: string): Theme {
  if (!name) return THEMES[DEFAULT_THEME]!;
  const t = THEMES[name.toLowerCase()];
  return t ?? THEMES[DEFAULT_THEME]!;
}

export const THEME_NAMES = Object.keys(THEMES);
