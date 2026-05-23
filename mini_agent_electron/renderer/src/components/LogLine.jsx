import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

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
// (viewBox="0 0 20 20", no script/event-handler attributes)
function isSafeEmojiSvg(svg) {
  if (!svg || typeof svg !== 'string') return false;
  // Must be a complete <svg>...</svg> element
  if (!/^<svg\s/.test(svg.trim()) || !/<\/svg>\s*$/.test(svg.trim())) return false;
  // Must have viewBox (our emoji SVGs all use viewBox="0 0 20 20")
  if (!/\bviewBox\s*=\s*["']0\s+0\s+20\s+20["']/.test(svg)) return false;
  // Reject dangerous constructs
  if (/<script\b/i.test(svg)) return false;
  if (/\bon\w+\s*=/i.test(svg)) return false;
  if (/javascript\s*:/i.test(svg)) return false;
  return true;
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
    return (
      <div className={`md-line ${line.cls || ''}`}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            p: ({ children }) => <span>{children}</span>,
          }}
        >
          {line.text}
        </ReactMarkdown>
      </div>
    );
  }

  // Plain text — HTML-escaped
  return <div className={line.cls || ''}>{escapeHtml(line.text)}</div>;
}
