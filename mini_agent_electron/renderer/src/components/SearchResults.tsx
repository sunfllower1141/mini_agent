import { useMemo } from 'react';

// search_files / rg format:
const SEARCH_LINE_RE = /^(.+):(\d+): ?(.*)$/;

function parseLine(line: string): Record<string, any> {
  // find_symbol format
  const symbolMatch = line.match(/^\s{2}(def|class|fn|const|let|var|function)\s+(.+?)\s+->\s+(.+?):(\d+)$/);
  if (symbolMatch) {
    return {
      kind: symbolMatch[1],
      symbol: symbolMatch[2],
      file: symbolMatch[3],
      lineno: symbolMatch[4],
    };
  }
  // search_files format
  const searchMatch = line.match(SEARCH_LINE_RE);
  if (searchMatch) {
    return {
      file: searchMatch[1],
      lineno: searchMatch[2],
      content: searchMatch[3],
    };
  }
  return { raw: line };
}

const CONTAINER_STYLE: React.CSSProperties = {
  padding: '6px 0',
  margin: '4px 0',
  maxWidth: '100%',
};

const ROW_STYLE: React.CSSProperties = {
  padding: '1px 0',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

const FILE_STYLE: React.CSSProperties = {
  color: '#6cc8e8',
};

const LINENO_SPAN: React.CSSProperties = {
  color: '#b8d975',
  userSelect: 'none',
};

const CONTENT_STYLE: React.CSSProperties = {
  color: '#e0e0e0',
};

const KIND_STYLE: React.CSSProperties = {
  color: '#ce9d7c',
};

const SYMBOL_STYLE: React.CSSProperties = {
  color: '#dcdcaa',
};

const RAW_STYLE: React.CSSProperties = {
  color: '#e0e0e0',
  padding: '1px 0',
};

interface SearchResultsProps {
  content?: string;
}

export default function SearchResults({ content }: SearchResultsProps) {
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
      {lines.map((entry: any) => {
        if (entry.raw !== undefined) {
          return (
            <div key={entry.key} style={RAW_STYLE}>
              {entry.raw}
            </div>
          );
        }
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
