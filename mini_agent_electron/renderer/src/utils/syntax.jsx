/**
 * Syntax highlighting components powered by prism-react-renderer v2.
 * Provides a <CodeBlock> for react-markdown's custom code renderer,
 * and a helper to highlight inline tool-call text.
 */
import { Highlight, themes } from 'prism-react-renderer';

/** Languages we probe when no language is specified. */
const PROBE_LANGUAGES = [
  'python',
  'javascript',
  'json',
  'typescript',
  'bash',
  'shell',
  'css',
  'sql',
  'yaml',
  'rust',
  'go',
  'markdown',
  'xml',
  'toml',
];

const theme = themes.nightOwl;

/**
 * Try to guess the language from text content using Prism tokenization.
 * Returns the language name that produces the richest highlighting,
 * or 'markdown' as a safe fallback.
 */
function detectLanguage(code) {
  const { Prism } = require('prism-react-renderer');
  let bestLang = 'markdown';
  let bestScore = 0;

  for (const lang of PROBE_LANGUAGES) {
    const grammar = Prism.languages[lang];
    if (!grammar) continue;
    try {
      // Tokenize and count non-text tokens as a signal of good highlighting
      const tokens = Prism.tokenize(code, grammar);
      let score = 0;
      function count(tok) {
        if (typeof tok === 'string') return;
        if (Array.isArray(tok)) { tok.forEach(count); return; }
        if (tok.type && tok.type !== 'plain') score++;
        if (tok.content) {
          if (typeof tok.content === 'string') return;
          if (Array.isArray(tok.content)) tok.content.forEach(count);
          else count(tok.content);
        }
      }
      count(tokens);
      if (score > bestScore) {
        bestScore = score;
        bestLang = lang;
      }
    } catch {
      // ignore languages that throw
    }
  }
  return bestLang;
}

/** Extract language from a markdown code fence className (e.g. "language-python" → "python"). */
function langFromClass(className) {
  if (!className) return null;
  const match = className.match(/language-(\w+)/);
  return match ? match[1] : null;
}

/**
 * Syntax-highlighted code block for use as react-markdown's <code> renderer.
 */
export function CodeBlock({ inline, className, children, ...props }) {
  const code = String(children).replace(/\n$/, '');
  const lang = langFromClass(className) || detectLanguage(code);

  if (inline) {
    return (
      <code
        style={{
          background: 'var(--surface0)',
          padding: '1px 5px',
          borderRadius: 4,
          fontFamily: 'var(--font-mono)',
          fontSize: '0.9em',
          color: 'var(--peach)',
        }}
        {...props}
      >
        {children}
      </code>
    );
  }

  return (
    <Highlight theme={theme} code={code} language={lang}>
      {({ style, tokens, getLineProps, getTokenProps }) => (
        <pre
          style={{
            ...style,
            background: 'var(--mantle)',
            padding: '12px 16px',
            borderRadius: 8,
            overflow: 'auto',
            margin: '8px 0',
            fontSize: '0.85em',
            lineHeight: 1.5,
          }}
        >
          {tokens.map((line, i) => (
            <div key={i} {...getLineProps({ line })} style={{ display: 'flex' }}>
              <span
                style={{
                  display: 'inline-block',
                  width: '2em',
                  userSelect: 'none',
                  opacity: 0.4,
                  textAlign: 'right',
                  marginRight: '1em',
                  flexShrink: 0,
                }}
              >
                {i + 1}
              </span>
              <span>
                {line.map((token, key) => (
                  <span key={key} {...getTokenProps({ token })} />
                ))}
              </span>
            </div>
          ))}
        </pre>
      )}
    </Highlight>
  );
}

/**
 * Highlight an inline tool-call summary like "read_file(path="/foo")".
 * Returns an HTML string — use with dangerouslySetInnerHTML.
 */
export function highlightInline(text) {
  const { Prism } = require('prism-react-renderer');
  const grammar = Prism.languages['python'] || Prism.languages['javascript'];
  if (!grammar) return escapeHtml(text);
  try {
    return Prism.highlight(text, grammar, 'python');
  } catch {
    return escapeHtml(text);
  }
}

function escapeHtml(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
