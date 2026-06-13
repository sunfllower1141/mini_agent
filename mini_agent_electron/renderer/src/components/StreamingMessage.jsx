import { useState, useEffect, useRef, memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * Renders streaming text with markdown.  ReactMarkdown is expensive, so we
 * throttle re-parses to ~80ms intervals.  Key insight: we ALWAYS render
 * ReactMarkdown -- we just update the text fed to it at a lower rate.
 * Toggling between <pre> and ReactMarkdown causes visible flicker because
 * the DOM structure changes (block -> inline reflow).
 *
 * The markdown view lags up to 80ms behind the incoming text, which is
 * imperceptible.  When streaming stops, a final flush catches it up.
 */
const StreamingMessage = memo(function StreamingMessage({ text }) {
  const [throttled, setThrottled] = useState('');
  const lastUpdateRef = useRef(0);
  const pendingRef = useRef(null);
  const timerRef = useRef(null);

  useEffect(() => {
    const now = performance.now();
    const elapsed = now - lastUpdateRef.current;

    if (elapsed >= 80) {
      lastUpdateRef.current = now;
      setThrottled(text);
    } else {
      // Store the latest text but don't render yet
      pendingRef.current = text;
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

    return () => {
      // Don't clear the timer here -- we want the deferred update to fire.
      // The timer cleans itself up.
    };
  }, [text]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  if (!text || !text.trim()) return null;

  // Always render ReactMarkdown with the throttled text.
  // When streaming ends, throttled === text and we show the final version.
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]}>
      {throttled || text}
    </ReactMarkdown>
  );
});

export default StreamingMessage;
