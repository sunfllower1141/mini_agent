import { useState, useEffect, useRef, useCallback } from 'react';

/**
 * useSmoothStream — buffer incoming text chunks and animate them
 * character-by-character at a smooth, consistent rate via requestAnimationFrame.
 *
 * Returns:
 *   displayedText  — current visible text (animating)
 *   addChunk       — call with each incoming token chunk
 *   reset          — clear the stream
 *   flush          — instantly display all buffered text
 */
export default function useSmoothStream({ speed = 8 } = {}) {
  const [displayedText, setDisplayedText] = useState('');
  const fullRef = useRef('');
  const indexRef = useRef(0);
  const rafRef = useRef(null);
  const lastRef = useRef(0);

  const animate = useCallback((time) => {
    const full = fullRef.current;
    if (indexRef.current < full.length) {
      if (time - lastRef.current > speed) {
        // Advance by 1-3 chars depending on how far behind we are
        const behind = full.length - indexRef.current;
        const step = behind > 100 ? 6 : behind > 30 ? 3 : 1;
        indexRef.current = Math.min(indexRef.current + step, full.length);
        setDisplayedText(full.slice(0, indexRef.current));
        lastRef.current = time;
      }
      rafRef.current = requestAnimationFrame(animate);
    } else {
      rafRef.current = null;
    }
  }, [speed]);

  const addChunk = useCallback((text) => {
    if (!text) return;
    fullRef.current += text;
    if (!rafRef.current) {
      lastRef.current = performance.now();
      rafRef.current = requestAnimationFrame(animate);
    }
  }, [animate]);

  const reset = useCallback(() => {
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    fullRef.current = '';
    indexRef.current = 0;
    setDisplayedText('');
  }, []);

  const flush = useCallback(() => {
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    indexRef.current = fullRef.current.length;
    setDisplayedText(fullRef.current);
    return fullRef.current;
  }, []);

  // Cleanup RAF on unmount to avoid setState on unmounted component
  useEffect(() => {
    return () => {
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, []);

  return { displayedText, addChunk, reset, flush };
}
