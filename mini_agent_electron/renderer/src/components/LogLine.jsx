import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import CodeBlock from './CodeBlock';
import ThinkingBlock from './ThinkingBlock';

const markdownComponents = {
  code({ className, children, inline, ...props }) {
    const match = /language-(\w+)/.exec(className || '');
    const lang = match ? match[1] : undefined;
    const code = String(children).replace(/\n$/, '');
    return <CodeBlock code={code} language={lang} inline={inline} highlight={false} />;
  },
};

/**
 * A single log line -- supports plain text, markdown,
 * and structured tool-name rendering.
 *
 * Security: We NEVER use dangerouslySetInnerHTML for LLM-generated content.
 */

// React auto-escapes content inside {}, so we only need to coerce to string.
// Manual HTML-escaping (the old version) caused double-escaping: " -> &quot;.
function escapeHtml(text) {
  if (!text) return '';
  return String(text);
}

const LogLine = memo(function LogLine({ line }) {
  // React component -- render directly
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

  // Markdown rendering (NO dangerouslySetInnerHTML -- LLM output is sanitised)
  if (line.markdown) {
    return (
      <div className={`md-line ${line.cls || ''}`} style={{ whiteSpace: 'normal' }}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            p: ({ children }) => <span>{children}</span>,
            ...markdownComponents,
          }}
        >
          {line.text}
        </ReactMarkdown>
      </div>
    );
  }

  // Collapsible thinking box (sits inline among tool lines)
  if (line.thinkingText !== undefined) {
    return <ThinkingBlock text={line.thinkingText} active={line.thinkingActive} />;
  }

  // Prompt separator -- bold section divider marking user prompt boundaries in tools panel
  if (line.cls === 'prompt-separator') {
    return (
      <div className="prompt-separator">
        <span className="prompt-separator-line" />
        <span className="prompt-separator-badge"><span className="prompt-separator-badge-text">{line.promptText}</span></span>
        <span className="prompt-separator-line" />
      </div>
    );

  }

  // Plain text -- HTML-escaped

  return <div className={line.cls || ''}>{escapeHtml(line.text)}</div>;
});

export default LogLine;
