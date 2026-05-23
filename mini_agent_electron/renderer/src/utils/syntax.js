/**
 * Lightweight regex-based syntax highlighter for tool output.
 */

const SYN_PATTERNS = [
  { re: /#[^\n]*/g,              cls: 'syn-comment' },
  { re: /\/\/[^\n]*/g,           cls: 'syn-comment' },
  { re: /"[^"]*"/g,              cls: 'syn-string' },
  { re: /'[^']*'/g,              cls: 'syn-string' },
  { re: /`[^`]*`/g,              cls: 'syn-string' },
  { re: /@\w+/g,                 cls: 'syn-decorator' },
  { re: /\b(def|class|return|import|from|if|else|elif|try|except|finally|with|as|for|while|in|not|and|or|is|lambda|yield|raise|pass|break|continue|async|await|function|const|let|var|export|default|new|throw|catch|typeof|instanceof)\b/g, cls: 'syn-keyword' },
  { re: /\b(True|False|None|true|false|null|undefined|NaN|Infinity)\b/g, cls: 'syn-boolean' },
  { re: /\b\d+\.?\d*\b/g,        cls: 'syn-number' },
  { re: /(?:^|\s)([~/][^\s,:;]*\/[^\s,:;]+)/g, cls: 'syn-path' },
];

export function highlightSyntax(text) {
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  const tokens = [];
  SYN_PATTERNS.forEach(({ re, cls }) => {
    html = html.replace(re, (match) => {
      const idx = tokens.length;
      tokens.push(`<span class="${cls}">${match}</span>`);
      return `\x00${idx}\x00`;
    });
  });

  return html.replace(/\x00(\d+)\x00/g, (_, i) => tokens[+i]);
}
