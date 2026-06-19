import { useMemo } from 'react';
import CodeBlock, { EXT_TO_LANG } from './CodeBlock';

// -- parser ------------------------------------------------------------------

// Read_file with line_numbers=true or hash_lines=true format:
//   line_numbers:  "  42  content"
//   hash_lines:    "  42 Vale1│ content"  (word anchor + box-drawing pipe)
// Group 1 = line number, Group 2 = optional word-anchor hash
const READFILE_HASHLINE_RE = /^\s*(\d+)(?:\s+(\w+)│)?\s?/;

// Extract file path from tool summary like:
//   read_file(E:\path\to\file.py)  — no quotes, just parens
const TOOL_PATH_RE = /read_file\((.+?)\)\s*$/;

function extractPath(toolName) {
  if (!toolName) return null;
  const m = toolName.match(TOOL_PATH_RE);
  return m ? m[1] : null;
}

function extToLang(filePath) {
  if (!filePath) return null;
  const name = filePath.replace(/\\/g, '/').split('/').pop() || '';
  const lower = name.toLowerCase();

  // Exact filename match (e.g. "Dockerfile", "Makefile")
  if (EXT_TO_LANG[lower]) return EXT_TO_LANG[lower];

  // Extension match (e.g. ".py" → "py")
  const dotIdx = name.lastIndexOf('.');
  const ext = dotIdx >= 0 ? name.slice(dotIdx + 1).toLowerCase() : '';

  // Special: .env files
  if (!ext && name.startsWith('.')) {
    if (EXT_TO_LANG[name]) return EXT_TO_LANG[name];
  }

  return EXT_TO_LANG[ext] || null;
}

// -- styles ------------------------------------------------------------------

const HEADER_STYLE = {
  display: 'flex',
  alignItems: 'center',
  gap: '0.5em',
  padding: '3px 0 0',
  fontSize: '0.75em',
  color: '#888',
  fontFamily: '"JetBrains Mono", "Fira Code", monospace',
};

const FILENAME_STYLE = {
  color: '#6cc8e8',
  fontWeight: 500,
};

// -- component ---------------------------------------------------------------

export default function ReadFileResult({ content, toolName }) {
  const { source, filePath, lang, startLine, lineHashes } = useMemo(() => {
    if (!content) return { source: '', filePath: null, lang: null, startLine: 1, lineHashes: [] };
    const path = extractPath(toolName);
    // Parse line numbers and optional hash anchors before stripping prefixes
    let startLine = 1;
    const lines = content.split('\n');
    const hashes = [];
    const firstMatch = lines[0]?.match(READFILE_HASHLINE_RE);
    if (firstMatch) {
      startLine = parseInt(firstMatch[1], 10);
    }
    // Strip the full line-number (and hash) prefix from each line,
    // collecting hashes for the gutter display.
    const stripped = lines
      .map((line) => {
        const m = line.match(READFILE_HASHLINE_RE);
        hashes.push(m ? m[2] || null : null);
        return line.replace(READFILE_HASHLINE_RE, '');
      })
      .join('\n');
    return { source: stripped, filePath: path, lang: extToLang(path), startLine, lineHashes: hashes };
  }, [content, toolName]);

  if (!source.trim()) return null;

  const displayName = filePath
    ? filePath.replace(/\\/g, '/').split('/').pop()
    : null;

  return (
    <div style={{ margin: '4px 0' }}>
      {displayName && (
        <div style={HEADER_STYLE}>
          <span>📄</span>
          <span style={FILENAME_STYLE}>{displayName}</span>
        </div>
      )}
      <CodeBlock
        code={source}
        fontSize="0.78em"
        language={lang}
        highlight={true}
        lineNumbers={true}
        startLine={startLine}
        lineHashes={lineHashes}
        wrap={true}
      />
    </div>
  );
}
