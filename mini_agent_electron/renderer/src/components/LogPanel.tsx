import { useRef, useState, useCallback, memo, ReactNode } from 'react';
import LogLine from './LogLine';
import useAutoScroll from '../hooks/useAutoScroll';

interface LogPanelProps {
  id?: string;
  className?: string;
  lines?: LogLine[];
  children?: ReactNode;
}

/**
 * Auto-scrolling log container -- memoized so it only re-renders when
 * its `lines` or `children` props actually change.
 */
const LogPanel = memo(function LogPanel({ id, className, lines, children }: LogPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
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
      {lines && lines.map((line, i) => <LogLine key={line._key || line.id || `ln-${i}`} line={line} />)}
      {children}
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
