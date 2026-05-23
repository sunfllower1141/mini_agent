import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * A single log line — supports plain text, icons, HTML, and markdown.
 */
export default function LogLine({ line }) {
  if (line.component) {
    return <div className={line.cls || ''}>{line.component}</div>;
  }
  if (line.html) {
    return <div className={line.cls || ''} dangerouslySetInnerHTML={{ __html: line.html }} />;
  }
  if (line.icon) {
    return <div className={line.cls || ''} dangerouslySetInnerHTML={{ __html: `${line.icon} ${line.text}` }} />;
  }
  if (line.markdown) {
    return (
      <div className={`md-line ${line.cls || ''}`}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          allowDangerousHtml={true}
          components={{
            p: ({ children }) => <span>{children}</span>,
          }}
        >
          {line.text}
        </ReactMarkdown>
      </div>
    );
  }
  // Plain text — if it contains SVG icons (from emoji replacement), render as HTML
  if (line.text && line.text.includes('<svg')) {
    return <div className={line.cls || ''} dangerouslySetInnerHTML={{ __html: line.text }} />;
  }
  return <div className={line.cls || ''}>{line.text}</div>;
}
