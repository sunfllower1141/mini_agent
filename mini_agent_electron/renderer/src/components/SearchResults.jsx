import { useMemo } from 'react';

// -- parser ------------------------------------------------------------------

// search_files / rg format:
//   "E:\\path\\file.py:42:  matched content"
//   "/unix/path/file.py:42:matched content"
//
// find_symbol format:
//   "  def    some_func  ->  E:\\path\\file.py:322"
//
// Greedy .+ then backtrack to last :\d+: to handle Windows drive-letter colons.
const SEARCH_LINE_RE = /^(.+):(\d+): ?(.*)$/;

// find_symbol lines: optional leading whitespace, kind, name, "->", path:line
const SYMBOL_LINE_RE = /^\s*(\S+)\s+(\S+)\s+->\s+(.+):(\d+)$/;

function parseLine(line) {
  // Try search_files format first
  let m = line.match(SEARCH_LINE_RE);
  if (m) {
    return { file: m[1], lineno: parseInt(m[2], 10), content: m[3] };
  }
  // Try find_symbol format
  m = line.match(SYMBOL_LINE_RE);
  if (m) {
    return {
      kind: m[1],
      symbol: m[2],
      file: m[3],
      lineno: parseInt(m[4], 10),
    };
  }
  return { raw: line };
}

// -- styles ------------------------------------------------------------------

const CONTAINER_STYLE = {
  padding: '6px 0',
  margin: '4px 0',
  maxWidth: '100%',
  fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
  fontSize: '0.82em',
  lineHeight: '1.65',
};

const ROW_STYLE = {
  padding: '1px 0',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

const FILE_STYLE = {
  color: '#6cc8e8',
};

const LINENO_SPAN = {
  color: '#b8d975',
  userSelect: 'none',
};

const CONTENT_STYLE = {
  color: '#e0e0e0',
};

const KIND_STYLE = {
  color: '#ce9d7c',
};

const SYMBOL_STYLE = {
  color: '#dcdcaa',
};

const RAW_STYLE = {
  color: '#e0e0e0',
  padding: '1px 0',
};

// -- component ---------------------------------------------------------------

export default function SearchResults({ content }) {
  const lines = useMemo(() => {
    if (!content) return [];
    return content.split('\n').map((line, i) => ({
      key: i,
      ...parseLine(line),
    }));
  }, [content]);

  if (lines.length === 0) return null;

  return (
    <div style={CONTAINER_STYLE}>
      {lines.map((entry) => {
        if (entry.raw !== undefined) {
          return (
            <div key={entry.key} style={RAW_STYLE}>
              {entry.raw}
            </div>
          );
        }
        // find_symbol format (has kind + symbol)
        if (entry.kind) {
          return (
            <div key={entry.key} style={ROW_STYLE}>
              <span style={KIND_STYLE}>{entry.kind}</span>
              {' '}
              <span style={SYMBOL_STYLE}>{entry.symbol}</span>
              {' -> '}
              <span style={FILE_STYLE}>{entry.file}</span>
              {':'}
              <span style={LINENO_SPAN}>{entry.lineno}</span>
            </div>
          );
        }
        // search_files format (file + lineno + content)
        return (
          <div key={entry.key} style={ROW_STYLE}>
            <span style={FILE_STYLE}>{entry.file}</span>
            {':'}
            <span style={LINENO_SPAN}>{entry.lineno}</span>
            {': '}
            <span style={CONTENT_STYLE}>{entry.content}</span>
          </div>
        );
      })}
    </div>
  );
}
