import { useState, memo, useEffect, useRef } from 'react';

const TYPING_SPEED = 6;

interface ThinkingBlockProps {
  text?: string;
  active?: boolean;
}

/**
 * Collapsible thinking… box that sits inline among tool calls.
 */
const ThinkingBlock = memo(function ThinkingBlock({ text = '', active = false }: ThinkingBlockProps) {
  const [open, setOpen] = useState(false);
  const [visible, setVisible] = useState(0);
  const wasEverActiveRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Track whether this block was ever streaming (active=true)
  if (active) wasEverActiveRef.current = true;

  useEffect(() => {
    // If this block was ever active (was streaming), show full text on replay
    if (wasEverActiveRef.current) {
      setVisible(text.length);
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
      return;
    }
    wasEverActiveRef.current = false;
    setVisible(0);
    if (timerRef.current) clearInterval(timerRef.current);

    if (!text) return;

    let i = 0;
    timerRef.current = setInterval(() => {
      i += 1;
      if (i >= text.length) {
        setVisible(text.length);
        clearInterval(timerRef.current!);
        timerRef.current = null;
      } else {
        setVisible(i);
      }
    }, TYPING_SPEED);

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [text, active]);

  const showFull = active || wasEverActiveRef.current;
  const revealed = showFull ? text : text.slice(0, visible);
  const typing = active ? false : visible < text.length;
  const cls = `thinking-box ${open ? 'open' : ''}${active ? ' thinking-active' : ''}`;
  const toggle = () => setOpen((o) => !o);

  return (
    <div className={cls}>
      {open && <div className="click-strip" onClick={toggle} />}
      <div
        className="thinking-box-header"
        onClick={toggle}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { toggle(); e.preventDefault(); } }}
        title={open ? 'Collapse thinking' : 'Expand thinking'}
        aria-label={open ? 'Collapse thinking' : 'Expand thinking'}
        aria-expanded={open}
      >
        <span className="thinking-box-label">thinking{typing ? '…' : ''}</span>
        {!open && (
          <span className="thinking-box-preview">
            {revealed || '\u200b'}
          </span>
        )}
      </div>
      {open && (
        <div className="thinking-box-body">
          {revealed || '\u200b'}
          {typing && <span className="thinking-cursor">|</span>}
        </div>
      )}
    </div>
  );
});

export default ThinkingBlock;
