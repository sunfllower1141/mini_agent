import { useState, useEffect, useRef } from 'react';
import { createHighlighter, createJavaScriptRegexEngine } from 'shiki';
import AnsiBlock from './AnsiBlock';

// -- comprehensive language set ----------------------------------------------
// Every language a coding agent realistically encounters.
// Shiki loads each as a WASM grammar on demand — only languages actually used
// cost memory in practice.  The list is kept broad so no file type goes plain.

const LANGS = [
  // code
  'python', 'javascript', 'typescript', 'bash', 'json', 'diff',
  'css', 'html', 'markdown', 'yaml', 'toml', 'xml', 'sql', 'jsonc',
  'rust', 'go', 'c', 'cpp', 'java', 'ruby', 'php', 'swift', 'kotlin',
  'tsx', 'jsx', 'vue', 'svelte', 'dart', 'elixir', 'clojure', 'lua',
  'scala', 'haskell', 'ocaml', 'nim', 'zig', 'r', 'perl', 'julia',
  // shell / config
  'shellscript', 'docker', 'make', 'ini', 'git-commit', 'git-rebase',
  'cmake', 'nginx', 'terraform', 'dotenv',
  // web
  'scss', 'less', 'graphql', 'gql', 'astro', 'vue-html', 'postcss',
  // windows
  'powershell', 'bat',
  // other common
  'viml', 'csv',
];

// -- language detection ------------------------------------------------------

const TOOL_LANG_MAP = {
  run_shell: 'bash', execute: 'python', python: 'python',
  node: 'javascript', bash: 'bash', sh: 'bash', shell: 'bash',
  zsh: 'bash', fish: 'bash',
};

/** Map a file extension to a Shiki language ID.  Keep in sync with LANGS. */
export const EXT_TO_LANG = {
  // code
  py: 'python', pyw: 'python', js: 'javascript', mjs: 'javascript',
  cjs: 'javascript', jsx: 'javascript', ts: 'typescript', mts: 'typescript',
  tsx: 'typescript', rs: 'rust', go: 'go', java: 'java', rb: 'ruby',
  php: 'php', swift: 'swift', kt: 'kotlin', kts: 'kotlin', scala: 'scala',
  clj: 'clojure', cljs: 'clojure', cljc: 'clojure', edn: 'clojure',
  hs: 'haskell', lhs: 'haskell', ml: 'ocaml', mli: 'ocaml', nim: 'nim',
  zig: 'zig', r: 'r', R: 'r', pl: 'perl', pm: 'perl', jl: 'julia',
  lua: 'lua', ex: 'elixir', exs: 'elixir', dart: 'dart',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp',
  hxx: 'cpp', hh: 'cpp',
  // config / data
  json: 'json', jsonc: 'jsonc', json5: 'json', yaml: 'yaml', yml: 'yaml',
  toml: 'toml', xml: 'xml', svg: 'xml', ini: 'ini', cfg: 'ini',
  conf: 'ini', properties: 'ini',
  env: 'dotenv', '.env': 'dotenv',
  // markup
  html: 'html', htm: 'html', css: 'css', scss: 'scss', sass: 'scss',
  less: 'less', md: 'markdown', mdx: 'markdown', markdown: 'markdown',
  vue: 'vue', svelte: 'svelte', astro: 'astro',
  // shell / ops
  sh: 'shellscript', bash: 'shellscript', zsh: 'shellscript',
  fish: 'shellscript', dockerfile: 'docker', makefile: 'make',
  mk: 'make', cmake: 'cmake', nginx: 'nginx', tf: 'terraform',
  tfvars: 'terraform', hcl: 'terraform',
  // sql
  sql: 'sql', psql: 'sql', mysql: 'sql', sqlite: 'sql',
  // misc
  diff: 'diff', patch: 'diff', graphql: 'gql', gql: 'gql',
  powershell: 'powershell', ps1: 'powershell', psm1: 'powershell',
  bat: 'bat', cmd: 'bat', vim: 'viml', vimrc: 'viml', csv: 'csv',
  tsv: 'csv',
  // git
  'git-commit': 'git-commit', 'git-rebase': 'git-rebase',
};

function guessLanguage(toolName, code) {
  const content = code || '';
  const firstLine = content.trimStart().split('\n')[0];

  // Shebang lines
  if (firstLine?.startsWith('#!')) {
    if (firstLine.includes('python')) return 'python';
    if (firstLine.includes('node')) return 'javascript';
    if (firstLine.includes('bash') || firstLine.includes('sh')) return 'shellscript';
    if (firstLine.includes('ruby')) return 'ruby';
    if (firstLine.includes('perl')) return 'perl';
    if (firstLine.includes('php')) return 'php';
    if (firstLine.includes('lua')) return 'lua';
  }

  // Python patterns (strong signals)
  const pyPatterns = [
    /^(from\s+\w+\s+import|import\s+\w+)/m,
    /^\s*(def\s+\w+\s*\(|class\s+\w+\s*[:\(])/m,
    /^\s*@\w+/m,
    /^\s*if\s+__name__\s*==/m,
    /^\s*async\s+def\s/m,
    /^\s*with\s+\w+/m,
  ];
  for (const p of pyPatterns) {
    if (p.test(content)) return 'python';
  }

  // Rust patterns
  if (/^\s*(fn\s+\w+|pub\s+fn|impl\s+\w+|use\s+\w+::)/m.test(content)) return 'rust';
  // Go patterns
  if (/^\s*(package\s+\w+|func\s+\w+|import\s+")/m.test(content)) return 'go';

  // Markup / data detection
  if (/^\s*<\?xml/.test(firstLine)) return 'xml';
  if (/^\s*<!DOCTYPE\s+html/i.test(firstLine) || /^\s*<html/i.test(firstLine))
    return 'html';

  // JSON (after XML/HTML check)
  if (/^\s*[{[].*"[^"]+"\s*:/.test(content.trimStart())) return 'json';

  // Tool-name mapping (after content checks so content-based beats tool name)
  if (toolName) {
    const lower = toolName.toLowerCase();
    for (const [key, lang] of Object.entries(TOOL_LANG_MAP)) {
      if (lower.includes(key)) return lang;
    }
  }

  return 'text';
}

// -- singleton highlighter --------------------------------------------------

let highlighterPromise = null;

function getHighlighter() {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({
      langs: LANGS,
      themes: ['dark-plus'],
      engine: createJavaScriptRegexEngine(),
    }).catch((err) => {
      // Reset so the next attempt can retry (e.g. after a hot-reload)
      highlighterPromise = null;
      console.warn('[CodeBlock] Shiki highlighter init failed:', err);
      throw err;
    });
  }
  return highlighterPromise;
}

// -- styles ------------------------------------------------------------------

const INLINE_CODE_STYLE = {
  display: 'inline',
  whiteSpace: 'normal',
  background: '#1a1a1a',
  color: '#ccc',
  padding: '1px 5px',
  borderRadius: '4px',
  fontSize: '0.9em',
  fontFamily: '"JetBrains Mono", "Fira Code", monospace',
};

// -- component ---------------------------------------------------------------

export default function CodeBlock({
  children,
  code,
  language,
  className = '',
  inline,
  highlight = true,
  fontSize,
  toolName,
  lineNumbers = false,
  startLine = 1,
  lineHashes = [],
  wrap = false,
}) {
  const source = code ?? children;

  const langFromClass = className?.startsWith('language-')
    ? className.slice('language-'.length)
    : null;
  const lang = language || langFromClass || guessLanguage(toolName, source);

  // inline code -- keep simple, no Shiki overhead
  if (inline) {
    if (!highlight) return <code>{source}</code>;
    return <code style={INLINE_CODE_STYLE}>{source}</code>;
  }

  if (!source || source.trim().length === 0) return null;

  // no highlighting -- plain block
  if (!highlight) {
    // If content contains ANSI escape codes, render with color
    if (source.indexOf('\x1b') !== -1) {
      return (
        <pre style={{
          padding: '4px 0', margin: '4px 0',
          overflowX: wrap ? 'hidden' : 'auto',
          fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
          lineHeight: '1.55', maxWidth: '100%',
          background: 'transparent',
        }}>
          <AnsiBlock text={source} style={{
            color: '#ccc',
            whiteSpace: wrap ? 'pre-wrap' : 'pre',
            wordBreak: wrap ? 'break-word' : 'normal',
            display: 'block',
          }} />
        </pre>
      );
    }
    return (
      <pre style={{
        padding: '4px 0', margin: '4px 0',
        overflowX: wrap ? 'hidden' : 'auto',
        fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
        lineHeight: '1.55', maxWidth: '100%',
      }}>
        <code style={{
          color: '#ccc',
          whiteSpace: wrap ? 'pre-wrap' : 'pre',
          wordBreak: wrap ? 'break-word' : 'normal',
          display: 'block',
        }}>
          {lineNumbers ? source.split('\n').map((l, i) => {
            const num = String(i + startLine).padStart(5, ' ');
            const hashStr = lineHashes[i] ? `:${lineHashes[i]}` : '';
            return `${num}${hashStr}  ${l}`;
          }).join('\n') : source}
        </code>
      </pre>
    );
  }

  // Shiki highlighting -- async codeToHtml
  return <ShikiBlock source={source} lang={lang} fontSize={fontSize} lineNumbers={lineNumbers} startLine={startLine} lineHashes={lineHashes} wrap={wrap} />;
}

// Strip the background color that shiki injects on the <pre>
// so the tool pane's own dark background shows through seamlessly.
function stripBg(html) {
  return html.replace(/(<pre[^>]*style=")background-color:#[0-9a-fA-F]+;?/g, '$1');
}

// -- ShikiBlock (handles the async highlighter lifecycle) --------------------

// Plain-text fallback when Shiki doesn't know a language
function PlainBlock({ source, fontSize, lineNumbers, startLine = 1, lineHashes = [], wrap }) {
  // If content contains ANSI escape codes, render with color
  if (source.indexOf('\x1b') !== -1) {
    const lines = source.split('\n');
    return (
      <pre style={{
        padding: '4px 0', margin: '4px 0',
        overflowX: wrap ? 'hidden' : 'auto',
        fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
        fontSize: fontSize || 'inherit', lineHeight: '1.55',
        background: 'transparent', borderRadius: '8px', maxWidth: '100%',
      }}>
        <code style={{
          color: '#ccc',
          whiteSpace: wrap ? 'pre-wrap' : 'pre',
          wordBreak: wrap ? 'break-word' : 'normal',
          display: 'block',
        }}>
          {lineNumbers
            ? lines.map((l, i) => {
                const num = String(i + startLine).padStart(4, '\u00A0');
                const hashStr = lineHashes[i] ? `:${lineHashes[i]}` : '';
                return (
                  <div key={i}>
                    <span className="shiki-ln" style={{ color:'#555', userSelect:'none', display:'inline-block', minWidth:'3em', textAlign:'right', marginRight:'0.5em' }}>
                      {num}{hashStr}
                    </span>
                    <AnsiBlock text={l} />
                  </div>
                );
              })
            : <AnsiBlock text={source} />
          }
        </code>
      </pre>
    );
  }
  const lines = source.split('\n');
  return (
    <pre style={{
      padding: '4px 0', margin: '4px 0',
      overflowX: wrap ? 'hidden' : 'auto',
      fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
      fontSize: fontSize || 'inherit', lineHeight: '1.55',
      background: 'transparent', borderRadius: '8px', maxWidth: '100%',
    }}>
      <code style={{
        color: '#ccc',
        whiteSpace: wrap ? 'pre-wrap' : 'pre',
        wordBreak: wrap ? 'break-word' : 'normal',
        display: 'block',
      }}>
        {lineNumbers
          ? lines.map((l, i) => {
              const num = String(i + startLine).padStart(4, '\u00A0');
              const hashStr = lineHashes[i] ? `:${lineHashes[i]}` : '';
              return (
                <div key={i}>
                  <span className="shiki-ln" style={{ color:'#555', userSelect:'none', display:'inline-block', minWidth:'3em', textAlign:'right', marginRight:'0.5em' }}>
                    {num}{hashStr}
                  </span>
                  {l}
                </div>
              );
            })
          : source}
      </code>
    </pre>
  );
}

function ShikiBlock({ source, lang, fontSize, lineNumbers, startLine = 1, lineHashes = [], wrap }) {
  const [html, setHtml] = useState(null);
  const [failed, setFailed] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;

    getHighlighter()
      .then((h) => {
        if (cancelled) return;
        try {
          const htmlStr = h.codeToHtml(source, {
            lang,
            theme: 'dark-plus',
          });
          if (!cancelled && mountedRef.current) setHtml(stripBg(htmlStr));
        } catch {
          // Unknown language — fall back to plain text
          if (!cancelled && mountedRef.current) setFailed(true);
        }
      })
      .catch(() => {
        // Highlighter failed to init (e.g. bad lang name) — fall back
        if (!cancelled && mountedRef.current) setFailed(true);
      });

    return () => { cancelled = true; };
  }, [source, lang]);

  useEffect(() => {
    return () => { mountedRef.current = false; };
  }, []);

  if (failed) {
    return <PlainBlock source={source} fontSize={fontSize} lineNumbers={lineNumbers} startLine={startLine} lineHashes={lineHashes} wrap={wrap} />;
  }

  if (!html) {
    // fallback while Shiki loads (first render only — highlighter is cached)
    return <PlainBlock source={source} fontSize={fontSize} lineNumbers={lineNumbers} startLine={startLine} lineHashes={lineHashes} wrap={wrap} />;
  }

  // Inject line numbers (and optional hash anchors) into each <span class="line">
  // so they stay paired with their logical line even when the code wraps.
  let htmlLineCounter = startLine - 1;
  const annotatedHtml = lineNumbers
    ? html.replace(/<span class="line"[^>]*>/g, (match) => {
        const idx = ++htmlLineCounter - startLine;
        const hash = lineHashes[idx] || '';
        const hashStr = hash ? `<span class="shiki-hash">:${hash}</span>` : '';
        const num = String(htmlLineCounter).padStart(4, '\u00A0');
        return `${match}<span class="shiki-ln">${num}${hashStr}  </span>`;
      })
    : html;

  return (
    <div className={`shiki-block${wrap ? ' shiki-wrap' : ''}`} style={{ background: 'transparent', ...(fontSize ? { fontSize } : {}) }}>
      {wrap && (
        <style>{`.shiki-wrap pre { white-space: pre-wrap !important; overflow-x: hidden !important; word-break: break-word; }`}</style>
      )}
      {lineNumbers && (
        <style>{`.shiki-ln { color:#555; user-select:none; display:inline-block; min-width:3em; text-align:right; margin-right:0.5em; }
.shiki-hash { color:#444; font-size:0.85em; }`}</style>
      )}
      <div
        style={{ overflowX: wrap ? 'hidden' : 'auto' }}
        dangerouslySetInnerHTML={{ __html: annotatedHtml }}
      />
    </div>
  );
}
