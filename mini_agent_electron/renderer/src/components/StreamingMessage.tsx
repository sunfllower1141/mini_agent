import { useState, useEffect, useRef, memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface StreamingMessageProps {
  text?: string;
}

/**
 * Renders streaming text with throttled markdown parsing.
 */
const StreamingMessage = memo(function StreamingMessage({ text }: StreamingMessageProps) {
  const [throttled, setThrottled] = useState('');
  const lastUpdateRef = useRef(0);
  const pendingRef = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const now = performance.now();
    const elapsed = now - lastUpdateRef.current;

    if (elapsed >= 80) {
      lastUpdateRef.current = now;
      setThrottled(text ?? '');
    } else {
      pendingRef.current = text ?? null;
      if (!timerRef.current) {
        const remaining = 80 - elapsed;
        timerRef.current = setTimeout(() => {
          timerRef.current = null;
          lastUpdateRef.current = performance.now();
          if (pendingRef.current !== null) {
            setThrottled(pendingRef.current);
            pendingRef.current = null;
          }
        }, remaining);
      }
    }
  }, [text]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  if (!text || !text.trim()) return null;

  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]}>
      {throttled || text}
    </ReactMarkdown>
  );
});

export default StreamingMessage;
