import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import CodeBlock from './CodeBlock';

const markdownComponents = {
  code({ className, children, inline, ...props }) {
    const match = /language-(\w+)/.exec(className || '');
    const lang = match ? match[1] : undefined;
    const code = String(children).replace(/\n$/, '');
    return <CodeBlock code={code} language={lang} inline={inline} highlight={false} />;
  },
};

/**
 * A single log line — supports plain text, SVG icons, markdown,
 * and structured tool-name rendering.
 *
 * Security: We NEVER use dangerouslySetInnerHTML for LLM-generated content.
 * The only exception is app-generated SVG icons (emoji replacement) which
 * are validated before rendering.
 */

// Simple HTML-escape for plain-text content
function escapeHtml(text) {
  if (!text) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Validate that an SVG string looks like an emoji icon from our backend
// (viewBox="0 0 24 24", no script/event-handler attributes)
function isSafeEmojiSvg(svg) {
  if (!svg || typeof svg !== 'string') return false;
  if (!/^<svg\s/.test(svg.trim()) || !/<\/svg>\s*$/.test(svg.trim())) return false;
  // Must have viewBox (our emoji SVGs use 0 0 24 24)
  if (!/\bviewBox\s*=\s*["']0\s+0\s+(?:20\s+20|24\s+24)["']/.test(svg)) return false;
  // Reject dangerous constructs
  if (/<script\b/i.test(svg)) return false;
  if (/\bon\w+\s*=/i.test(svg)) return false;
  if (/javascript\s*:/i.test(svg)) return false;
  return true;
}

// Extract an SVG icon from the start of a text string.
// Returns { svgIcon, text } if found, or null.
export function extractSvgFromText(text) {
  if (!text || typeof text !== 'string') return null;
  const m = text.match(/^(<svg\b[^>]*>.*?<\/svg>)\s*(.*)$/s);
  if (!m) return null;
  const svg = m[1];
  const rest = m[2] || '';
  if (!isSafeEmojiSvg(svg)) return null;
  return { svgIcon: svg, text: rest };
}


export default function LogLine({ line }) {
  // React component — render directly
  if (line.component) {
    return <div className={line.cls || ''}>{line.component}</div>;
  }

  // Structured tool name (replaces the old dangerouslySetInnerHTML for html)
  if (line.toolName) {
    return (
      <div className={line.cls || ''}>
        <span className="accent">{line.toolName}</span>
        {line.toolArgs && <span className="dim">{line.toolArgs}</span>}
      </div>
    );
  }

  // SVG icon (only used for app-generated emoji icons — validated for safety)
  if (line.svgIcon && isSafeEmojiSvg(line.svgIcon)) {
    return (
      <div className={line.cls || ''}>
        <span
          className="emoji-icon"
          dangerouslySetInnerHTML={{ __html: line.svgIcon }}
        />
        <span>{escapeHtml(line.text)}</span>
      </div>
    );
  }

  // Markdown rendering (NO allowDangerousHtml — LLM output is sanitised)
  if (line.markdown) {
    const extracted = extractSvgFromText(line.text);
    const mdText = extracted ? extracted.text : line.text;
    return (
      <div className={`md-line ${line.cls || ''}`} style={{ whiteSpace: 'normal' }}>
        {extracted && (
          <span
            className="emoji-icon"
            dangerouslySetInnerHTML={{ __html: extracted.svgIcon }}
          />
        )}
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            p: ({ children }) => <span>{children}</span>,
            ...markdownComponents,
          }}
        >
          {mdText}
        </ReactMarkdown>
      </div>
    );
  }

  // Try to extract an embedded SVG icon from the text before plain rendering
  const extracted = extractSvgFromText(line.text);
  if (extracted) {
    return (
      <div className={line.cls || ''}>
        <span
          className="emoji-icon"
          dangerouslySetInnerHTML={{ __html: extracted.svgIcon }}
        />
        <span>{escapeHtml(extracted.text)}</span>
      </div>
    );
  }

  // Plain text — HTML-escaped
  return <div className={line.cls || ''}>{escapeHtml(line.text)}</div>;
}
