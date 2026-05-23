/**
 * Syntax highlighter powered by Prism (via prism-react-renderer).
 * Tries multiple languages and picks the one with the most token matches.
 */
import { Prism } from 'prism-react-renderer';

/** Languages we probe, ordered by preference (common first). */
const PROBE_LANGUAGES = [
  'javascript',
  'python',
  'json',
  'typescript',
  'css',
  'sql',
  'markup',
  'yaml',
  'rust',
  'go',
  'markdown',
];

/** Escape HTML characters. */
function escapeHtml(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/**
 * Highlight text and return an HTML string.
 * Tries every language in PROBE_LANGUAGES and keeps the result
 * that produces the most <span> tags (i.e. the richest highlighting).
 * Falls back to plain escaped text if nothing matches.
 */
export function highlightSyntax(text) {
  let bestHtml = null;
  let bestScore = 0;

  for (const lang of PROBE_LANGUAGES) {
    const grammar = Prism.languages[lang];
    if (!grammar) continue;
    try {
      const html = Prism.highlight(text, grammar, lang);
      // Count <span> tags as a rough measure of highlighting richness
      const spanCount = (html.match(/<span /g) || []).length;
      if (spanCount > bestScore) {
        bestScore = spanCount;
        bestHtml = html;
      }
    } catch {
      // ignore languages that throw
    }
  }

  // Require at least 2 spans to consider it "highlighted" —
  // avoids false positives from a single punctuation span
  if (bestHtml && bestScore >= 2) {
    return bestHtml;
  }

  return escapeHtml(text);
}
