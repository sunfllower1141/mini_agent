import { useMemo } from 'react';
import anser from 'anser';

// -- ANSI-to-color mapping for React inline styles ---------------------------

const ANSI_COLORS = {
  // Standard colors
  '0': null,             // reset
  '1': { fontWeight: 'bold' },
  '2': { opacity: 0.55 },  // dim
  '3': { fontStyle: 'italic' },
  '4': { textDecoration: 'underline' },
  '30': { color: '#111' },   // black
  '31': { color: '#f44747' }, // red
  '32': { color: '#6a9955' }, // green
  '33': { color: '#dcdcaa' }, // yellow
  '34': { color: '#569cd6' }, // blue
  '35': { color: '#c586c0' }, // magenta
  '36': { color: '#4ec9b0' }, // cyan
  '37': { color: '#d4d4d4' }, // white
  '90': { color: '#666' },    // bright black (gray)
  '91': { color: '#f44747' }, // bright red
  '92': { color: '#6a9955' }, // bright green
  '93': { color: '#dcdcaa' }, // bright yellow
  '94': { color: '#569cd6' }, // bright blue
  '95': { color: '#c586c0' }, // bright magenta
  '96': { color: '#4ec9b0' }, // bright cyan
  '97': { color: '#e0e0e0' }, // bright white
};

function mergeStyles(codes) {
  if (!codes || codes.length === 0) return {};
  const style = {};
  for (const code of codes) {
    const s = ANSI_COLORS[code];
    if (s) Object.assign(style, s);
  }
  return style;
}

function ansiToElements(text) {
  if (!text) return null;

  // Fast path: no ESC character
  if (text.indexOf('\x1b') === -1) {
    return text;
  }

  try {
    const json = anser.ansiToJson(text, { use_classes: false });
    if (!json || json.length === 0) return text;

    return json.map((seg, i) => {
      const style = {
        color: seg.fg ? ANSI_COLORS[seg.fg]?.color || seg.fg : undefined,
        backgroundColor: seg.bg ? ANSI_COLORS[seg.bg]?.color || seg.bg : undefined,
        fontWeight: seg.decoration === 'bold' || seg.decoration === 'bold' ? 'bold' : undefined,
        opacity: seg.decoration === 'dim' ? 0.55 : undefined,
        fontStyle: seg.decoration === 'italic' ? 'italic' : undefined,
        textDecoration: seg.decoration === 'underline' ? 'underline' : undefined,
      };
      // Remove undefined keys
      for (const k of Object.keys(style)) {
        if (style[k] === undefined) delete style[k];
      }
      if (Object.keys(style).length === 0) {
        return <span key={i}>{seg.content}</span>;
      }
      return <span key={i} style={style}>{seg.content}</span>;
    });
  } catch {
    // If anser fails, strip ANSI and return plain text
    return text.replace(/\x1b\[[0-9;]*m/g, '');
  }
}

// -- Component ----------------------------------------------------------------

/**
 * Renders text containing ANSI escape codes as colored React spans.
 * If no ANSI codes are present, returns the text as-is (no wrapper).
 */
export default function AnsiBlock({ text, style = {}, className = '' }) {
  const elements = useMemo(() => ansiToElements(text), [text]);

  if (elements === null) return null;
  if (typeof elements === 'string') {
    return <span style={style} className={className}>{elements}</span>;
  }

  return (
    <span style={{ ...style, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }} className={className}>
      {elements}
    </span>
  );
}

export { ansiToElements };
