import { useState, useEffect, useCallback, useRef } from 'react';

const SCROLL_THRESHOLD = 40; // px from bottom = "at bottom"

/**
 * Auto-scroll hook that yields control to the user: when they scroll up
 * to read history, auto-scroll pauses. A button appears to jump back down.
 *
 * @param {React.RefObject} containerRef - ref to the scrollable element
 * @param {Array} deps - dependencies that trigger auto-scroll (like content)
 * @returns {{ isAtBottom: boolean, scrollToBottom: () => void }}
 */
export default function useAutoScroll(containerRef, deps = []) {
  const [isAtBottom, setIsAtBottom] = useState(true);
  const userScrolledRef = useRef(false);

  // Detect manual scroll
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const onScroll = () => {
      const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      const atBottom = distFromBottom <= SCROLL_THRESHOLD;
      setIsAtBottom(atBottom);

      // If user scrolled to bottom manually, resume auto-scroll
      if (atBottom) userScrolledRef.current = false;
      else userScrolledRef.current = true;
    };

    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [containerRef]);

  // Auto-scroll on content change — but only if pinned to bottom
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    // Always auto-scroll unless user has explicitly scrolled up
    if (!userScrolledRef.current) {
      el.scrollTop = el.scrollHeight;
      setIsAtBottom(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    userScrolledRef.current = false;
    el.scrollTop = el.scrollHeight;
    setIsAtBottom(true);
  }, [containerRef]);

  return { isAtBottom, scrollToBottom };
}
