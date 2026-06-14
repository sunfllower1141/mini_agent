import { useState, useEffect, useRef, useCallback } from 'react';

/**
 * useSmoothStream -- buffer incoming text chunks and animate them
 * with a smooth exponential catch-up at ~60 fps.  Each tick advances
 * by ceil(behind / 4), so the animation is fast when far behind and
 * slows naturally as it catches up -- no jarring discrete thresholds.
 *
 * Returns:
 *   displayedText  -- current visible text (animating)
 *   addChunk       -- call with each incoming token chunk
 *   reset          -- clear the stream
 *   flush          -- instantly display all buffered text
 */
export default function useSmoothStream() {
  const [displayedText, setDisplayedText] = useState('');
  const fullRef = useRef('');
  const indexRef = useRef(0);
  const timerRef = useRef(null);

  // ~60 fps -- smooth to the eye
  const TICK_MS = 16;

  // Use a ref to hold the latest tick function so addChunk can always
  // schedule the current version without stale-closure issues.
  const tickRef = useRef(null);

  tickRef.current = () => {
    const full = fullRef.current;
    const behind = full.length - indexRef.current;
    if (behind <= 0) {
      timerRef.current = null;
      return;
    }
    // Smooth exponential catch-up: advance by ceil(behind / 4).
    // Far behind -> big jumps.  Close -> 1 char per tick.
    const step = Math.max(1, Math.ceil(behind / 4));
    indexRef.current = Math.min(indexRef.current + step, full.length);
    setDisplayedText(full.slice(0, indexRef.current));

    // Schedule next tick if still behind
    if (indexRef.current < full.length) {
      timerRef.current = setTimeout(tickRef.current, TICK_MS);
    } else {
      timerRef.current = null;
    }
  };

  const addChunk = useCallback((text) => {
    if (!text) return;
    fullRef.current += text;
    if (!timerRef.current) {
      timerRef.current = setTimeout(tickRef.current, TICK_MS);
    }
  }, []);

  const reset = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    fullRef.current = '';
    indexRef.current = 0;
    setDisplayedText('');
  }, []);

  const flush = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    indexRef.current = fullRef.current.length;
    setDisplayedText(fullRef.current);
    return fullRef.current;
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  return { displayedText, addChunk, reset, flush };
}
