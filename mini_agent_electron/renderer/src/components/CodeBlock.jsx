import { useMemo } from 'react';

// ── language detection ────────────────────────────────────────────────────

const TOOL_LANG_MAP = {
  run_shell: 'bash', execute: 'python', python: 'python',
  node: 'javascript', bash: 'bash', sh: 'bash',
};

const EXT_LANG_MAP = {
  '.py':'python', '.pyx':'python', '.pyi':'python',
  '.js':'javascript', '.jsx':'javascript', '.mjs':'javascript', '.cjs':'javascript',
  '.ts':'typescript', '.tsx':'typescript', '.mts':'typescript',
  '.json':'json', '.md':'markdown',
  '.css':'css', '.scss':'css', '.less':'css',
  '.html':'html', '.xml':'html',
  '.sh':'bash', '.bash':'bash', '.zsh':'bash',
  '.yaml':'yaml', '.yml':'yaml',
  '.sql':'sql', '.rs':'rust', '.go':'go', '.java':'java',
  '.c':'c', '.cpp':'cpp', '.rb':'ruby',
  '.toml':'toml', '.ini':'ini', '.diff':'diff', '.patch':'diff',
};

function detectLanguage(toolName, code) {
  if (toolName) {
    const lower = toolName.toLowerCase();
    for (const [key, lang] of Object.entries(TOOL_LANG_MAP))
      if (lower.includes(key)) return lang;
    const extMatch = toolName.match(/\.([a-zA-Z0-9]+)(?:\s|\)|$|,)/);
    if (extMatch) {
      const ext = '.' + extMatch[1];
      if (EXT_LANG_MAP[ext]) return EXT_LANG_MAP[ext];
    }
  }

  const firstLine = (code || '').trimStart().split('\n')[0];
  if (firstLine?.startsWith('#!')) {
    if (firstLine.includes('python')) return 'python';
    if (firstLine.includes('node')) return 'javascript';
    if (firstLine.includes('bash') || firstLine.includes('sh')) return 'bash';
  }
  if (/^\s*<\?xml/.test(firstLine) || /^\s*<!DOCTYPE\s+html/i.test(firstLine)) return 'html';
  if (/^\s*[{[].*"[^"]+"\s*:/.test((code || '').trimStart())) return 'json';

  return 'text';
}

// ── simple tokenizer ──────────────────────────────────────────────────────

// Catppuccin Mocha colors
const T = {
  kw:   '#CBA6F7',  // mauve — keywords
  fn:   '#89B4FA',  // blue — function calls
  str:  '#A6E3A1',  // green — strings
  num:  '#FAB387',  // peach — numbers
  cmt:  '#6C7086',  // overlay2 — comments
  type: '#F9E2AF',  // yellow — class names/types
  op:   '#89DCEB',  // sky — operators
  builtin: '#F38BA8', // red — builtins
  plain:'#CDD6F4',  // text
};

const LANG_TOKENS = {
  python: {
    keywords: /\b(def|class|return|if|elif|else|for|while|import|from|as|with|try|except|finally|raise|yield|lambda|pass|break|continue|and|or|not|in|is|assert|del|global|nonlocal|async|await)\b/g,
    builtins: /\b(print|len|range|int|str|float|list|dict|set|tuple|bool|True|False|None|type|isinstance|open|enumerate|zip|map|filter|sorted|reversed|any|all|super|self|cls|Exception|ValueError|TypeError|KeyError|IndexError)\b/g,
    decorator: /(@\w+)/g,
    string: /("""[\s\S]*?"""|'''[\s\S]*?'''|"[^"]*"|'[^']*'|f"[^"]*"|f'[^']*'|b"[^"]*"|b'[^']*')/g,
    comment: /(#[^\n]*)/g,
    number: /\b(\d+\.?\d*)\b/g,
  },
  javascript: {
    keywords: /\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|try|catch|finally|throw|new|delete|typeof|instanceof|in|of|class|extends|import|export|default|from|as|async|await|yield|static|get|set|this|super|void|with|debugger)\b/g,
    builtins: /\b(console|document|window|Math|JSON|Promise|Array|Object|String|Number|Boolean|Map|Set|Symbol|undefined|null|true|false|NaN|Infinity|parseInt|parseFloat|require|module|process)\b/g,
    string: /(`[\s\S]*?`|"[^"]*"|'[^']*')/g,
    comment: /(\/\/[^\n]*|\/\*[\s\S]*?\*\/)/g,
    number: /\b(\d+\.?\d*[eE]?\d*)\b/g,
  },
  typescript: {
    keywords: /\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|try|catch|finally|throw|new|delete|typeof|instanceof|in|of|class|extends|implements|import|export|default|from|as|async|await|yield|static|get|set|this|super|type|interface|enum|namespace|declare|abstract|readonly|private|protected|public|keyof|infer|never|unknown|any)\b/g,
    builtins: /\b(console|document|window|Math|JSON|Promise|Array|Object|String|Number|Boolean|Map|Set|Symbol|undefined|null|true|false|NaN|Infinity|parseInt|parseFloat)\b/g,
    string: /(`[\s\S]*?`|"[^"]*"|'[^']*')/g,
    comment: /(\/\/[^\n]*|\/\*[\s\S]*?\*\/)/g,
    number: /\b(\d+\.?\d*[eE]?\d*)\b/g,
  },
  bash: {
    keywords: /\b(if|then|else|elif|fi|for|while|do|done|case|esac|in|function|return|local|export|source|exit|echo|printf|read|cd|ls|cp|mv|rm|mkdir|rmdir|chmod|chown|grep|sed|awk|cat|head|tail|sort|uniq|wc|find|xargs|curl|wget|tar|gzip|git|npm|yarn|docker|ssh|scp|rsync|true|false)\b/g,
    string: /("[^"]*"|'[^']*')/g,
    comment: /(#[^\n]*)/g,
    variable: /(\$[\w{}]+)/g,
  },
};

// ── tokenization ──────────────────────────────────────────────────────────

function tokenize(code, lang) {
  const tokens = LANG_TOKENS[lang] || LANG_TOKENS.python;
  const rules = [];

  // build sorted rule list
  if (tokens.comment) rules.push({ re: tokens.comment, color: T.cmt });
  if (tokens.string)  rules.push({ re: tokens.string,  color: T.str });
  if (tokens.keywords)rules.push({ re: tokens.keywords,color: T.kw });
  if (tokens.builtins)rules.push({ re: tokens.builtins,color: T.builtin });
  if (tokens.decorator)rules.push({ re: tokens.decorator,color: T.type });
  if (tokens.variable)rules.push({ re: tokens.variable,color: T.fn });
  if (tokens.number)  rules.push({ re: tokens.number,  color: T.num });

  // scan and produce spans
  const spans = [];
  let pos = 0;

  while (pos < code.length) {
    let earliest = null;
    let earliestRule = null;

    for (const rule of rules) {
      rule.re.lastIndex = pos;
      const m = rule.re.exec(code);
      if (m && m.index === pos) {
        if (!earliest || m[0].length > earliest[0].length) {
          earliest = m;
          earliestRule = rule;
        }
      }
    }

    if (earliest) {
      spans.push({ text: earliest[0], color: earliestRule.color });
      pos += earliest[0].length;
    } else {
      // take plain text up to next match
      let nextPos = code.length;
      for (const rule of rules) {
        rule.re.lastIndex = pos;
        const m = rule.re.exec(code);
        if (m && m.index < nextPos) nextPos = m.index;
      }
      if (nextPos <= pos) nextPos = pos + 1;
      spans.push({ text: code.slice(pos, nextPos), color: T.plain });
      pos = nextPos;
    }
  }

  return spans;
}

// ── CodeBlock component ───────────────────────────────────────────────────

const BLOCK_STYLE = {
  padding: '4px 0',
  margin: '4px 0',
  overflowX: 'auto',
  fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
  lineHeight: '1.55',
  maxWidth: '100%',
};

const CODE_STYLE = {
  color: '#CDD6F4',
  whiteSpace: 'pre',
  display: 'block',
};

const INLINE_CODE_STYLE = {
  display: 'inline',
  whiteSpace: 'normal',
  background: '#313244',
  color: '#F5C2E7',
  padding: '1px 5px',
  borderRadius: '4px',
  fontSize: '0.9em',
  fontFamily: '"JetBrains Mono", "Fira Code", monospace',
};

const LANG_BADGE_STYLE = {
  display: 'block',
  fontSize: '10px',
  color: '#6C7086',
  marginBottom: '4px',
  textTransform: 'uppercase',
  letterSpacing: '0.5px',
};

export default function CodeBlock({ children, code, language, className = '', inline, highlight = true, fontSize }) {
  const source = code ?? children;

  const langFromClass = className?.startsWith('language-')
    ? className.slice('language-'.length) : null;

  const lang = useMemo(
    () => language || langFromClass || detectLanguage(null, source || ''),
    [language, langFromClass, source]
  );

  // inline code
  if (inline) {
    if (!highlight) return <code>{source}</code>;
    return <code style={INLINE_CODE_STYLE}>{source}</code>;
  }

  if (!source || source.trim().length === 0) return null;

  // no syntax highlighting — plain block code
  if (!highlight) {
    return (
      <pre style={BLOCK_STYLE}>
        <code style={CODE_STYLE}>{source}</code>
      </pre>
    );
  }

  // tokenize and render
  const spans = useMemo(() => tokenize(source, lang), [source, lang]);

  return (
    <div style={{
      position: 'relative',
      margin: '8px 0',
      ...(fontSize ? { fontSize } : {}),
    }}>
      <pre style={BLOCK_STYLE}>
        {lang !== 'text' && (
          <span style={LANG_BADGE_STYLE}>{lang}</span>
        )}
        <code style={CODE_STYLE}>
          {spans.map((s, i) => (
            <span key={i} style={{ color: s.color }}>{s.text}</span>
          ))}
        </code>
      </pre>
    </div>
  );
}
