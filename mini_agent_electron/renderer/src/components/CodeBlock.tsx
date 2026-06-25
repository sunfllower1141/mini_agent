/**
 * CodeBlock.tsx -- Syntax-highlighted code blocks using Shiki.
 *
 * Renders either inline code, plain code blocks, or Shiki-highlighted blocks.
 * ANSI escape codes are rendered via AnsiBlock when highlighting is off.
 */
import { useState, useEffect, useRef, ReactNode } from 'react';
import { createHighlighter, createJavaScriptRegexEngine } from 'shiki';
import AnsiBlock from './AnsiBlock';

// -- comprehensive language set ----------------------------------------------
const LANGS = [
  'python', 'javascript', 'typescript', 'bash', 'json', 'diff',
  'css', 'html', 'markdown', 'yaml', 'toml', 'xml', 'sql', 'jsonc',
  'rust', 'go', 'c', 'cpp', 'java', 'ruby', 'php', 'swift', 'kotlin',
  'tsx', 'jsx', 'vue', 'svelte', 'dart', 'elixir', 'clojure', 'lua',
  'scala', 'haskell', 'ocaml', 'nim', 'zig', 'r', 'perl', 'julia',
  'shellscript', 'docker', 'make', 'ini', 'git-commit', 'git-rebase',
  'cmake', 'nginx', 'terraform', 'dotenv',
  'scss', 'less', 'graphql', 'gql', 'astro', 'vue-html', 'postcss',
  'powershell', 'bat',
  'viml', 'csv',
];

const TOOL_LANG_MAP: Record<string, string> = {
  run_shell: 'bash', execute: 'python', python: 'python',
  node: 'javascript', bash: 'bash', sh: 'bash', shell: 'bash',
  zsh: 'bash', fish: 'bash',
};

export const EXT_TO_LANG: Record<string, string> = {
  py: 'python', pyw: 'python', js: 'javascript', mjs: 'javascript',
  cjs: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
  css: 'css', html: 'html', json: 'json', yaml: 'yaml', yml: 'yaml',
  md: 'markdown', mdx: 'markdown', rs: 'rust', go: 'go',
  c: 'c', cpp: 'cpp', h: 'c', hpp: 'cpp',
  java: 'java', rb: 'ruby', php: 'php', swift: 'swift', kt: 'kotlin',
  sh: 'bash', bash: 'bash', zsh: 'bash', ps1: 'powershell', bat: 'bat',
  toml: 'toml', xml: 'xml', sql: 'sql', r: 'r', dart: 'dart',
  lua: 'lua', scala: 'scala', hs: 'haskell', ex: 'elixir', exs: 'elixir',
  clj: 'clojure', vue: 'vue', svelte: 'svelte',
  conf: 'ini', env: 'dotenv', ini: 'ini', cfg: 'ini',
  vim: 'viml', vimrc: 'viml',
  tf: 'terraform', hcl: 'terraform',
  diff: 'diff', patch: 'diff',
};

function extToLang(filePath: string | null): string | null {
  if (!filePath) return null;
  const ext = filePath.split('.').pop()?.toLowerCase();
  return ext ? EXT_TO_LANG[ext] || null : null;
}

function guessLanguage(toolName: string | undefined, content: string): string {
  const firstLine = content.trimStart().split('\n')[0];

  // Shebang
  if (firstLine && firstLine.startsWith('#!')) {
    if (/python/i.test(firstLine)) return 'python';
    if (/node/i.test(firstLine)) return 'javascript';
    if (/bash|sh|zsh/i.test(firstLine)) return 'bash';
    if (/ruby/i.test(firstLine)) return 'ruby';
    if (/perl/i.test(firstLine)) return 'perl';
  }

  // XML/HTML detection
  if (/^\s*<\?xml/.test(firstLine)) return 'xml';
  if (/^\s*<!DOCTYPE\s+html/i.test(firstLine) || /^\s*<html/i.test(firstLine))
    return 'html';

  // JSON
  if (/^\s*[{[].*"[^"]+"\s*:/.test(content.trimStart())) return 'json';

  // Tool-name mapping
  if (toolName) {
    const lower = toolName.toLowerCase();
    for (const [key, lang] of Object.entries(TOOL_LANG_MAP)) {
      if (lower.includes(key)) return lang;
    }
  }

  return 'text';
}

// -- singleton highlighter --------------------------------------------------
let highlighterPromise: Promise<any> | null = null;

function getHighlighter() {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({
      langs: LANGS as any,
      themes: ['dark-plus'],
      engine: createJavaScriptRegexEngine(),
    }).catch((err: Error) => {
      highlighterPromise = null;
      console.warn('[CodeBlock] Shiki highlighter init failed:', err);
      throw err;
    });
  }
  return highlighterPromise;
}

// -- styles ------------------------------------------------------------------
const INLINE_CODE_STYLE: React.CSSProperties = {
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
interface CodeBlockProps {
  children?: ReactNode;
  code?: string;
  language?: string;
  className?: string;
  inline?: boolean;
  highlight?: boolean;
  fontSize?: string;
  toolName?: string;
  lineNumbers?: boolean;
  startLine?: number;
  lineHashes?: string[];
  wrap?: boolean;
}

export default function CodeBlock(props: CodeBlockProps) {
  const {
    children, code, language, className = '', inline, highlight = true,
    fontSize, toolName, lineNumbers = false, startLine = 1, lineHashes = [], wrap = false,
  } = props;
  const source = code ?? (typeof children === 'string' ? children : '');

  const langFromClass = className?.startsWith('language-')
    ? className.slice('language-'.length)
    : null;
  const lang = language || langFromClass || guessLanguage(toolName, source as string);

  if (inline) {
    if (!highlight) return <code>{source}</code>;
    return <code style={INLINE_CODE_STYLE}>{source}</code>;
  }

  if (!source || (source as string).trim().length === 0) return null;

  if (!highlight) {
    const src = source as string;
    if (src.indexOf('\x1b') !== -1) {
      return (
        <pre style={{
          padding: '4px 0', margin: '4px 0',
          overflowX: wrap ? 'hidden' : 'auto',
          fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
          lineHeight: '1.55', maxWidth: '100%',
          background: 'transparent',
        }}>
          <AnsiBlock text={src} style={{
            color: '#ccc',
            whiteSpace: wrap ? 'pre-wrap' : 'pre',
            wordBreak: wrap ? 'break-word' : 'normal',
            display: 'block',
          } as React.CSSProperties} />
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
          {lineNumbers ? src.split('\n').map((l, i) => {
            const num = String(i + startLine).padStart(5, ' ');
            const hashStr = lineHashes[i] ? `:${lineHashes[i]}` : '';
            return `${num}${hashStr}  ${l}`;
          }).join('\n') : src}
        </code>
      </pre>
    );
  }

  return (
    <ShikiBlock
      source={source as string}
      lang={lang}
      fontSize={fontSize}
      lineNumbers={lineNumbers}
      startLine={startLine}
      lineHashes={lineHashes}
      wrap={wrap}
    />
  );
}

function stripBg(html: string): string {
  return html.replace(/(<pre[^>]*style=")background-color:#[0-9a-fA-F]+;?/g, '$1');
}

// -- PlainBlock fallback ----------------------------------------------------
function PlainBlock({ source, fontSize, lineNumbers, startLine = 1, lineHashes = [], wrap }: {
  source: string; fontSize?: string; lineNumbers?: boolean; startLine?: number; lineHashes?: string[]; wrap?: boolean;
}) {
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
                    <span style={{ color:'#555', userSelect:'none', display:'inline-block', minWidth:'3em', textAlign:'right', marginRight:'0.5em' }}>
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
              return `${num}${hashStr}  ${l}`;
            }).join('\n')
          : source
        }
      </code>
    </pre>
  );
}

// -- ShikiBlock (async highlighter) -----------------------------------------
function ShikiBlock({ source, lang, fontSize, lineNumbers, startLine, lineHashes, wrap }: {
  source: string; lang: string; fontSize?: string; lineNumbers?: boolean; startLine?: number; lineHashes?: string[]; wrap?: boolean;
}) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    let cancelled = false;
    getHighlighter().then((highlighter) => {
      if (cancelled || !mountedRef.current) return;
      try {
        const langSupported = highlighter.getLoadedLanguages().includes(lang);
        if (!langSupported) {
          setHtml(null);
          return;
        }
        const result = highlighter.codeToHtml(source, {
          lang,
          theme: 'dark-plus',
        });
        if (!cancelled) setHtml(stripBg(result));
      } catch (e) {
        if (!cancelled) setError(e as Error);
      }
    }).catch((e: Error) => {
      if (!cancelled) setError(e);
    });
    return () => { cancelled = true; };
  }, [source, lang]);

  if (error) {
    return <PlainBlock source={source} fontSize={fontSize} lineNumbers={lineNumbers} startLine={startLine} lineHashes={lineHashes} wrap={wrap} />;
  }

  if (html) {
    return <div dangerouslySetInnerHTML={{ __html: html }} style={{ fontSize: fontSize || 'inherit' }} />;
  }

  return <PlainBlock source={source} fontSize={fontSize} lineNumbers={lineNumbers} startLine={startLine} lineHashes={lineHashes} wrap={wrap} />;
}
