import { memo } from 'react';
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
 * A single log line — supports plain text, markdown,
 * and structured tool-name rendering.
 *
 * Security: We NEVER use dangerouslySetInnerHTML for LLM-generated content.
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

const LogLine = memo(function LogLine({ line }) {
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

  // Markdown rendering (NO dangerouslySetInnerHTML — LLM output is sanitised)
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

  // Plain text — HTML-escaped
  return <div className={line.cls || ''}>{escapeHtml(line.text)}</div>;
});

export default LogLine;
