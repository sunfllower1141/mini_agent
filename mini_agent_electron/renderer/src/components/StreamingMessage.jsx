import { useState, useEffect, useRef, memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * Renders streaming text with markdown, but throttles parses to ~80ms
 * intervals. ReactMarkdown is expensive — parsing every tick from
 * useSmoothStream burns CPU for zero visual gain. Humans can't
 * perceive markdown formatting changes at >12 fps.
 *
 * The raw text is always shown; markdown parsing catches up at ~12 fps.
 */
const StreamingMessage = memo(function StreamingMessage({ text }) {
  const [parsedAt, setParsedAt] = useState('');
  const lastParseRef = useRef(0);
  const timerRef = useRef(null);

  useEffect(() => {
    const now = performance.now();
    const elapsed = now - lastParseRef.current;

    if (elapsed >= 80) {
      // Enough time since last parse — update immediately
      lastParseRef.current = now;
      setParsedAt(text);
    } else {
      // Schedule an update after the remaining throttle window
      const remaining = 80 - elapsed;
      timerRef.current = setTimeout(() => {
        lastParseRef.current = performance.now();
        setParsedAt(text);
      }, remaining);
    }

    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [text]);

  if (!text || !text.trim()) return null;

  // Show raw <pre> text in the fast path; ReactMarkdown only renders when
  // parsedAt has caught up (throttled to ~12 fps by the effect above).
  if (parsedAt !== text) {
    return (
      <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit', fontSize: 'inherit' }}>
        {text}
      </pre>
    );
  }

  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]}>
      {parsedAt}
    </ReactMarkdown>
  );
});

export default StreamingMessage;
