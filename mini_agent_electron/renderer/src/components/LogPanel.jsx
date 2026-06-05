import { useRef, useEffect, memo } from 'react';
import LogLine from './LogLine';

/**
 * Auto-scrolling log container — memoized so it only re-renders when
 * its `lines` or `children` props actually change, not on every parent tick.
 */
const LogPanel = memo(function LogPanel({ id, className, lines, children }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines, children]);
  return (
    <div id={id} ref={ref} className={`log ${className || ''}`}>
      {lines && lines.map((line, i) => <LogLine key={i} line={line} />)}
      {children}
    </div>
  );
});

export default LogPanel;
