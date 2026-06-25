import { useState, useEffect, memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface DeferredMarkdownProps {
  text?: string;
  markdown?: boolean;
}

/**
 * Shows raw text in a <pre> instantly, then swaps to ReactMarkdown on next frame.
 */
const DeferredMarkdown = memo(function DeferredMarkdown({ text, markdown = true }: DeferredMarkdownProps) {
  const [parsed, setParsed] = useState<string | null>(null);

  useEffect(() => {
    if (!markdown) return;
    const id = requestAnimationFrame(() => setParsed(text ?? null));
    return () => cancelAnimationFrame(id);
  }, [text, markdown]);

  if (!text || !text.trim()) return null;

  if (!markdown) {
    return (
      <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit', fontSize: 'inherit' }}>
        {text}
      </pre>
    );
  }

  if (!parsed) {
    return (
      <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit', fontSize: 'inherit' }}>
        {text}
      </pre>
    );
  }

  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]}>
      {parsed}
    </ReactMarkdown>
  );
});

export default DeferredMarkdown;
