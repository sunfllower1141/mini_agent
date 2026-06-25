import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import CodeBlock from './CodeBlock';
import ThinkingBlock from './ThinkingBlock';

const markdownComponents: Record<string, React.ComponentType<any>> = {
  code({ className, children, inline, ...props }: any) {
    const match = /language-(\w+)/.exec(className || '');
    const lang = match ? match[1] : undefined;
    const code = String(children).replace(/\n$/, '');
    return (
      <CodeBlock code={code} language={lang} inline={inline} highlight={false} {...props} />
    );
  },
};

function escapeHtml(text: string): string {
  if (!text) return '';
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(text));
  return div.innerHTML;
}

interface LogLineProps {
  line: LogLine;
}

const LogLine = memo(function LogLine({ line }: LogLineProps) {
  // Inline component (e.g. ReadFileResult, SearchResults)
  if (line.component) {
    return <div className={line.cls || ''}>{line.component}</div>;
  }

  // Structured tool name
  if (line.toolName) {
    return (
      <div className={line.cls || ''}>
        <span className="accent">{line.toolName}</span>
        {line.toolArgs && <span className="dim">{line.toolArgs}</span>}
      </div>
    );
  }

  // Markdown rendering
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
          {line.text ?? ''}
        </ReactMarkdown>
      </div>
    );
  }

  // Collapsible thinking box
  if (line.thinkingText !== undefined) {
    return <ThinkingBlock text={line.thinkingText} active={line.thinkingActive} />;
  }

  // Prompt separator
  if (line.cls === 'prompt-separator') {
    return (
      <div className="prompt-separator">
        <span className="prompt-separator-line" />
        <span className="prompt-separator-badge"><span className="prompt-separator-badge-text">{line.promptText}</span></span>
        <span className="prompt-separator-line" />
      </div>
    );
  }

  // Plain text
  return <div className={line.cls || ''}>{escapeHtml(line.text ?? '')}</div>;
});

export default LogLine;
