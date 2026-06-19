import { useMemo } from 'react';
import CodeBlock, { EXT_TO_LANG } from './CodeBlock';

// -- parser ------------------------------------------------------------------

// Same format as read_file(hash_lines=True):
//   "  42 Vale1│ content"  (line number + optional word-anchor hash + box-drawing pipe)
const HASHLINE_RE = /^\s*(\d+)(?:\s+(\w+)\u2502)?\s?/;

// Header formats produced by AST tools:
//   get_file_skeleton:  "--- path/to/file.py ---"
//   get_function:       "===...--- E:\path\to\file.py :: func_name ---"
//   get_symbol_range:   "--- path/to/file.py :: symbol_name ---"
//   replace_symbol:     similar
//
// Optional metadata lines after header:
//   "[Function Hash: abc123]"  for get_function
//   "[Symbol: ...]"  for get_symbol_range
const HEADER_RE = /^[=-]+\s+(.+?)\s+[-]+$/;
const FUNC_LINE_RE = /^[=-]+\s+(.+?)\s+::\s+(.+?)\s+[-]+$/;
const META_RE = /^\[(Function Hash|Symbol|Symbol Hash):\s*(.+?)\]$/;

// Compaction markers injected by the backend when output is too large:
//   "... [compacted 2519 chars / ~629 tokens] ..."
const COMPACTION_RE = /^\.\.\.\s*\[compacted\s+\d+\s*(?:chars|token)/;

function extToLang(filePath) {
  if (!filePath) return null;
  const name = filePath.replace(/\\/g, '/').split('/').pop() || '';
  const lower = name.toLowerCase();
  if (EXT_TO_LANG[lower]) return EXT_TO_LANG[lower];
  const dotIdx = name.lastIndexOf('.');
  const ext = dotIdx >= 0 ? name.slice(dotIdx + 1).toLowerCase() : '';
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

const FUNCNAME_STYLE = {
  color: '#dcdcaa',
  fontWeight: 500,
};

const SEPARATOR_STYLE = {
  color: '#555',
};

const META_STYLE = {
  fontSize: '0.72em',
  color: '#666',
  fontFamily: '"JetBrains Mono", "Fira Code", monospace',
  padding: '1px 0 2px',
};

const COMPACTION_STYLE = {
  padding: '4px 8px',
  color: '#888',
  fontSize: '0.78em',
  fontStyle: 'italic',
  fontFamily: '"JetBrains Mono", "Fira Code", monospace',
  background: '#1a1a1a',
  borderRadius: '4px',
  margin: '2px 0',
  display: 'inline-block',
};

// -- component ---------------------------------------------------------------

/**
 * Renders AST tool output (get_file_skeleton, get_function, get_symbol_range,
 * replace_symbol) with a dark-grey gutter showing line numbers and word-anchor
 * hashes — identical to the read_file(hash_lines=True) display.
 */
export default function AstResult({ content, toolName }) {
  const parsed = useMemo(() => {
    if (!content) return { source: '', filePath: null, funcName: null, meta: null, lang: null, startLine: 1, lineHashes: [] };

    const lines = content.split('\n');

    // -- Parse header ------------------------------------------------
    let headerIdx = 0;
    let filePath = null;
    let funcName = null;
    let meta = null;

    // First non-empty line might be a separator/header line
    for (let i = 0; i < Math.min(lines.length, 3); i++) {
      const line = lines[i];
      // Check for function header: "--- path :: funcname ---"
      const funcMatch = line.match(FUNC_LINE_RE);
      if (funcMatch) {
        filePath = funcMatch[1].trim();
        funcName = funcMatch[2].trim();
        headerIdx = i;
        break;
      }
      // Check for plain header: "--- path ---"
      const headerMatch = line.match(HEADER_RE);
      if (headerMatch) {
        filePath = headerMatch[1].trim();
        headerIdx = i;
        break;
      }
      // Check for metadata line: "[Function Hash: ...]"
      const metaMatch = line.match(META_RE);
      if (metaMatch) {
        meta = { key: metaMatch[1], value: metaMatch[2] };
        headerIdx = i;
        continue; // may have header after meta, but typically meta follows header
      }
      // If we've seen a header or meta, stop; otherwise continue scanning
      if (filePath || meta) break;
    }

    // -- Scan for META lines immediately after the header line ----------
    // e.g. get_function output has [Function Hash: ...] right after the --- header
    let metaScanIdx = headerIdx + 1;
    while (metaScanIdx < lines.length) {
      const line = lines[metaScanIdx];
      const metaMatch = line.match(META_RE);
      if (metaMatch) {
        meta = { key: metaMatch[1], value: metaMatch[2] };
        headerIdx = metaScanIdx;  // advance headerIdx past meta line
        metaScanIdx++;
      } else if (line.trim() === '') {
        metaScanIdx++;
      } else {
        break;
      }
    }

    // -- Parse hash-anchored code lines -------------------------------
    // Start from after the header lines (skip blank line after header too)
    let codeStart = headerIdx + 1;
    while (codeStart < lines.length && lines[codeStart].trim() === '') {
      codeStart++;
    }

    let startLine = 0;
    const hashes = [];
    const strippedLines = [];

    for (let i = codeStart; i < lines.length; i++) {
      const line = lines[i];
      const m = line.match(HASHLINE_RE);
      if (m) {
        const lineNum = parseInt(m[1], 10);
        if (startLine === 0) startLine = lineNum;
        hashes.push(m[2] || null);
        strippedLines.push(line.replace(HASHLINE_RE, ''));
      } else if (COMPACTION_RE.test(line)) {
        // Compaction marker — keep as-is, no hash
        hashes.push(null);
        strippedLines.push(line);
      } else {
        // Non-matching line — could be continuation of previous line
        // or a compaction/separator line
        hashes.push(null);
        strippedLines.push(line);
      }
    }

    const source = strippedLines.join('\n');
    const lang = extToLang(filePath);

    return { source, filePath, funcName, meta, lang, startLine: startLine || 1, lineHashes: hashes };
  }, [content]);

  const { source, filePath, funcName, meta, lang, startLine, lineHashes } = parsed;

  if (!source.trim()) return null;

  const displayName = filePath
    ? filePath.replace(/\\/g, '/').split('/').pop()
    : null;

  return (
    <div style={{ margin: '4px 0' }}>
      {/* Header: file icon + filename + optional :: funcname */}
      {(filePath || funcName) && (
        <div style={HEADER_STYLE}>
          <span>📄</span>
          {displayName && (
            <span style={FILENAME_STYLE}>{displayName}</span>
          )}
          {funcName && (
            <>
              <span style={SEPARATOR_STYLE}>::</span>
              <span style={FUNCNAME_STYLE}>{funcName}</span>
            </>
          )}
        </div>
      )}
      {/* Metadata line: [Function Hash: abc123] */}
      {meta && (
        <div style={META_STYLE}>
          [{meta.key}: {meta.value}]
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
