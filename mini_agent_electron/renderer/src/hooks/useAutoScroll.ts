import { useState, useEffect, useCallback, useRef, DependencyList } from 'react';

const SCROLL_THRESHOLD = 40;

interface UseAutoScrollResult {
  isAtBottom: boolean;
  scrollToBottom: () => void;
}

/**
 * Auto-scroll hook that yields control to the user: when they scroll up
 * to read history, auto-scroll pauses. A button appears to jump back down.
 */
export default function useAutoScroll(
  containerRef: React.RefObject<HTMLDivElement | null>,
  deps: DependencyList = []
): UseAutoScrollResult {
  const [isAtBottom, setIsAtBottom] = useState(true);
  const userScrolledRef = useRef(false);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const onScroll = () => {
      const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      const atBottom = distFromBottom <= SCROLL_THRESHOLD;
      setIsAtBottom(atBottom);
      if (atBottom) userScrolledRef.current = false;
      else userScrolledRef.current = true;
    };

    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [containerRef]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
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
