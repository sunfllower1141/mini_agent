import { useState, useEffect, useRef, useCallback } from 'react';

const TICK_MS = 16; // ~60 fps

interface UseSmoothStreamResult {
  displayedText: string;
  addChunk: (text: string) => void;
  reset: () => void;
  flush: () => string;
}

/**
 * useSmoothStream -- buffer incoming text chunks and animate them
 * with a smooth exponential catch-up at ~60 fps.
 */
export default function useSmoothStream(): UseSmoothStreamResult {
  const [displayedText, setDisplayedText] = useState('');
  const fullRef = useRef('');
  const indexRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tickRef = useRef<(() => void) | null>(null);

  const tick = useCallback(() => {
    const full = fullRef.current;
    const behind = full.length - indexRef.current;
    if (behind <= 0) {
      timerRef.current = null;
      return;
    }
    const step = Math.max(1, Math.ceil(behind / 4));
    indexRef.current = Math.min(indexRef.current + step, full.length);
    setDisplayedText(full.slice(0, indexRef.current));
    if (indexRef.current < full.length) {
      timerRef.current = setTimeout(tickRef.current!, TICK_MS);
    } else {
      timerRef.current = null;
    }
  }, []);

  tickRef.current = tick;

  const addChunk = useCallback((text: string) => {
    if (!text) return;
    fullRef.current += text;
    if (!timerRef.current) {
      timerRef.current = setTimeout(tickRef.current!, TICK_MS);
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
