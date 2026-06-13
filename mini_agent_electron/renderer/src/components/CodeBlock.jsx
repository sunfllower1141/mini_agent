import { useState, useEffect, useRef } from 'react';
import { createHighlighter, createJavaScriptRegexEngine } from 'shiki';

// -- language detection ------------------------------------------------------

const TOOL_LANG_MAP = {
  run_shell: 'bash', execute: 'python', python: 'python',
  node: 'javascript', bash: 'bash', sh: 'bash',
};

function guessLanguage(toolName, code) {
  // Content-based detection first -- overrides tool name mapping
  const content = code || '';
  const firstLine = content.trimStart().split('\n')[0];

  // Shebang lines
  if (firstLine?.startsWith('#!')) {
    if (firstLine.includes('python')) return 'python';
    if (firstLine.includes('node')) return 'javascript';
    if (firstLine.includes('bash') || firstLine.includes('sh')) return 'bash';
  }

  // Python patterns (strong signals -- keywords that bash scripts don't use)
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

  // Tool-name mapping (after content checks so python in run_shell wins)
  if (toolName) {
    const lower = toolName.toLowerCase();
    for (const [key, lang] of Object.entries(TOOL_LANG_MAP)) {
      if (lower.includes(key)) return lang;
    }
  }

  if (/^\s*<\?xml/.test(firstLine) || /^\s*<!DOCTYPE\s+html/i.test(firstLine)) return 'html';
  if (/^\s*[{[].*"[^"]+"\s*:/.test(content.trimStart())) return 'json';
  return 'text';
}

// -- singleton highlighter --------------------------------------------------

let highlighterPromise = null;

function getHighlighter() {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({
      // Only the languages a coding agent actually encounters.
      // Each grammar is a WASM blob -- fewer = less memory.
      langs: ['python', 'javascript', 'typescript', 'bash', 'json', 'diff'],
      themes: ['dark-plus'],
      engine: createJavaScriptRegexEngine(),
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
    return (
      <pre style={{
        padding: '4px 0', margin: '4px 0', overflowX: 'auto',
        fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
        lineHeight: '1.55', maxWidth: '100%',
      }}>
        <code style={{ color: '#ccc', whiteSpace: 'pre', display: 'block' }}>
          {source}
        </code>
      </pre>
    );
  }

  // Shiki highlighting -- async codeToHtml
  return <ShikiBlock source={source} lang={lang} fontSize={fontSize} />;
}

// Strip the background color that shiki injects on the <pre>
// so the tool pane's own dark background shows through seamlessly.
function stripBg(html) {
  return html.replace(/(<pre[^>]*style=")background-color:#[0-9a-fA-F]+;?/g, '$1');
}

// -- ShikiBlock (handles the async highlighter lifecycle) --------------------

function ShikiBlock({ source, lang, fontSize }) {
  const [html, setHtml] = useState(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    let cancelled = false;

    getHighlighter().then((h) => {
      if (cancelled) return;
      const result = h.codeToHtml(source, {
        lang,
        theme: 'dark-plus',
      });
      if (result && typeof result.then === 'function') {
        result.then((htmlStr) => {
          if (!cancelled && mountedRef.current) setHtml(stripBg(htmlStr));
        });
      } else if (result && !cancelled && mountedRef.current) {
        setHtml(stripBg(result));
      }
    });

    return () => { cancelled = true; };
  }, [source, lang]);

  useEffect(() => {
    return () => { mountedRef.current = false; };
  }, []);

  if (!html) {
    // fallback while Shiki loads (first render only -- highlighter is cached)
    return (
      <pre style={{
        padding: '12px 16px', margin: '8px 0', overflowX: 'auto',
        fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
        fontSize: fontSize || 'inherit', lineHeight: '1.55',
        background: 'transparent', borderRadius: '8px', maxWidth: '100%',
      }}>
        <code style={{ color: '#ccc', whiteSpace: 'pre', display: 'block' }}>
          {source}
        </code>
      </pre>
    );
  }

  return (
    <div
      className="shiki-block"
      style={{ background: 'transparent', ...(fontSize ? { fontSize } : {}) }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
