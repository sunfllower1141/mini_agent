import { useState, memo, useEffect, useRef } from 'react';

/** TYPING_SPEED: ms between revealing each character (typewriter feel). */
const TYPING_SPEED = 6;

/**
 * Collapsible thinking… box that sits inline among tool calls.
 * Click the header to expand/collapse the full thinking text.
 *
 * Features:
 *  - Typewriter animation: text reveals character-by-character.
 *  - Collapsed: shows 2 lines then truncates with CSS line-clamp.
 *  - Expanded: full text visible, typing continues.
 */
const ThinkingBlock = memo(function ThinkingBlock({ text, active }) {
  const [open, setOpen] = useState(false);
  const [visible, setVisible] = useState(0);
  const timerRef = useRef(null);
  const prevActiveRef = useRef(false);

  // --- Typewriter animation (only when not actively streaming) ---
  useEffect(() => {
    const wasActive = prevActiveRef.current;
    prevActiveRef.current = active;

    // During active streaming, show text immediately — no typewriter
    if (active) {
      setVisible(text.length);
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
      return;
    }

    // Just transitioned from active → inactive (streaming ended).
    // Text was already fully displayed; don't replay.
    if (wasActive) {
      setVisible(text.length);
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
      return;
    }

    // Typewriter for brand-new or text-changed-while-inactive thinking blocks
    setVisible(0);
    if (timerRef.current) clearInterval(timerRef.current);

    if (!text) return;

    let i = 0;
    timerRef.current = setInterval(() => {
      i += 1;
      if (i >= text.length) {
        setVisible(text.length);
        clearInterval(timerRef.current);
        timerRef.current = null;
      } else {
        setVisible(i);
      }
    }, TYPING_SPEED);

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [text, active]);

  // When active (streaming), show all text; otherwise use typewriter progress
  const revealed = active ? text : text.slice(0, visible);
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
        <span className="thinking-box-label">thinking{typing ? '\u2026' : ''}</span>
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
