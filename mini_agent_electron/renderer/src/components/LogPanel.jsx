import { useRef, useState, useCallback, memo } from 'react';
import LogLine from './LogLine';
import useAutoScroll from '../hooks/useAutoScroll';

/**
 * Auto-scrolling log container -- memoized so it only re-renders when
 * its `lines` or `children` props actually change, not on every parent tick.
 *
 * When the user scrolls up to read history, auto-scroll pauses and a
 * "↓ Scroll to latest" button appears. Auto-scroll resumes when the
 * user scrolls back to the bottom or clicks the button.
 */
const LogPanel = memo(function LogPanel({ id, className, lines, children }) {
  const containerRef = useRef(null);
  const { isAtBottom, scrollToBottom } = useAutoScroll(containerRef, [lines, children]);
  const [hovering, setHovering] = useState(false);

  const onMouseEnter = useCallback(() => setHovering(true), []);
  const onMouseLeave = useCallback(() => setHovering(false), []);

  const showJump = hovering && !isAtBottom;

  return (
    <div
      id={id}
      ref={containerRef}
      className={`log ${className || ''}`}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      {lines && lines.map((line, i) => <LogLine key={i} line={line} />)}
      {children}
      {/* Scroll-to-bottom button */}
      {showJump && (
        <button
          className="scroll-jump-btn"
          onClick={scrollToBottom}
          title="Scroll to latest"
          aria-label="Scroll to latest output"
        >
          ↓
        </button>
      )}
    </div>
  );
});

export default LogPanel;

