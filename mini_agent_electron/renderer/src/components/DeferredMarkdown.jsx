import { useState, useEffect, memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * Shows raw text in a <pre> instantly (zero parse cost), then swaps to
 * ReactMarkdown on the next frame.  This prevents the synchronous
 * ReactMarkdown parse from blocking the main thread on large blocks.
 *
 * For thinking blocks, set markdown={false} to stay as plain <pre> forever.
 */
const DeferredMarkdown = memo(function DeferredMarkdown({ text, markdown = true }) {
  const [parsed, setParsed] = useState(null);

  useEffect(() => {
    if (!markdown) return;
    const id = requestAnimationFrame(() => setParsed(text));
    return () => cancelAnimationFrame(id);
  }, [text, markdown]);

  if (!text || !text.trim()) return null;

  // Plain pre mode -- used for thinking blocks
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
