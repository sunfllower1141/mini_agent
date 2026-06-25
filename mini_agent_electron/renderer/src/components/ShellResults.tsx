import { useMemo } from 'react';
import AnsiBlock from './AnsiBlock';

// -- styles ------------------------------------------------------------------

const CONTAINER_STYLE = {
  padding: '6px 0',
  margin: '4px 0',
  maxWidth: '100%',
  fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
  fontSize: '0.82em',
  lineHeight: '1.65',
};

const STATUS_ROW_STYLE = {
  display: 'flex',
  gap: '0.5em',
  alignItems: 'center',
  marginBottom: '4px',
};

const STATUS_BADGE_STYLE = (ok) => ({
  display: 'inline-block',
  padding: '0px 7px',
  borderRadius: '3px',
  fontSize: '0.75em',
  fontWeight: 600,
  color: '#fff',
  background: ok ? '#3a7d44' : '#b04040',
  textTransform: 'uppercase',
  letterSpacing: '0.5px',
});

const OUTPUT_LINE_STYLE = {
  color: '#d4d4d4',
  padding: '1px 0',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

const DIM_LINE_STYLE = {
  color: '#888',
  padding: '1px 0',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

const LINE_NO_SPAN = {
  color: '#555',
  userSelect: 'none',
  display: 'inline-block',
  minWidth: '3em',
  textAlign: 'right',
  marginRight: '0.5em',
};

// -- component ---------------------------------------------------------------

export default function ShellResults({ content, ok }) {
  const lines = useMemo(() => {
    if (!content) return [];
    return content.split('\n');
  }, [content]);

  if (lines.length === 0) return null;

  return (
    <div style={CONTAINER_STYLE}>
      <div style={STATUS_ROW_STYLE}>
        <span style={STATUS_BADGE_STYLE(ok)}>{ok ? 'OK' : 'ERR'}</span>
        <span style={{ color: '#999', fontSize: '0.78em' }}>
          exit={ok ? '0' : '≠0'}
        </span>
      </div>
      {lines.map((line, i) => {
        // Dim empty lines and lines that look like separators
        const isDim = line === '' || /^-{3}$/.test(line.trim());
        const lineNum = String(i + 1).padStart(4, '\u00A0');
        return (
          <div key={i} style={isDim ? DIM_LINE_STYLE : OUTPUT_LINE_STYLE}>
            <span style={LINE_NO_SPAN}>{lineNum}  </span>
            {line ? <AnsiBlock text={line} /> : '\u00A0'}
          </div>
        );
      })}
    </div>
  );
}
