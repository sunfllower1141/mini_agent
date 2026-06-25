import { useState, useRef, useCallback, memo, ReactNode } from 'react';

interface ToolCallBoxProps {
  toolName: string;
  toolArgs?: string;
  ok?: boolean | null;
  running?: boolean;
  children?: ReactNode;
}

/**
 * Collapsible tool call box.
 * Collapsed by default — header shows tool name + args + preview.
 * Click header to expand; text selection is preserved (won't toggle).
 */
const ToolCallBox = memo(function ToolCallBox({
  toolName, toolArgs, ok, running = false, children
}: ToolCallBoxProps) {
  const [open, setOpen] = useState(false);
  const selRef = useRef(false);

  const handleClick = useCallback(() => {
    if (selRef.current) {
      selRef.current = false;
      return;
    }
    setOpen((o) => !o);
  }, []);

  const handleMouseDown = useCallback(() => {
    selRef.current = false;
  }, []);

  const handleMouseUp = useCallback(() => {
    const sel = window.getSelection();
    if (sel) selRef.current = (sel.type === 'Range' && sel.toString().length > 0);
  }, []);

  const cls = running
    ? 'tool-call-box tool-running'
    : `tool-call-box ${ok ? 'tool-ok' : 'tool-err'}`;

  return (
    <div className={`${cls} ${open ? 'open' : ''}`}>
      {open && <div className="click-strip" onClick={handleClick} />}
      <div
        className="tool-call-header"
        onClick={handleClick}
        onMouseDown={handleMouseDown}
        onMouseUp={handleMouseUp}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { handleClick(); e.preventDefault(); } }}
        title={open ? 'Collapse tool call' : 'Expand tool call'}
        aria-label={open ? 'Collapse tool call' : 'Expand tool call'}
        aria-expanded={open}
      >
        <span className="accent">{toolName}</span>
        {toolArgs && <span className="dim">{toolArgs}</span>}
        {running && <span className="tool-call-spinner" />}
        {!open && !running && (
          <span className={`tool-call-preview ${ok ? 'ok' : 'err'}`}>
            {ok ? 'OK' : 'ERR'}
          </span>
        )}
      </div>
      {open && (
        <div className="tool-call-body">
          {children}
        </div>
      )}
    </div>
  );
});

export default ToolCallBox;
