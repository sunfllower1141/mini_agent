import { useState, useRef, useEffect, useCallback } from 'react';

/**
 * Clickable session label in the footer. On click, shows a dropdown with
 * available sessions and a "New session…" option.
 */
export default function SessionPicker({ sessionName, onSwitch }) {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [current, setCurrent] = useState(sessionName || 'default');
  const [showNewInput, setShowNewInput] = useState(false);
  const [newName, setNewName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const dropdownRef = useRef(null);
  const inputRef = useRef(null);

  // Sync external session name
  useEffect(() => {
    if (sessionName) setCurrent(sessionName);
  }, [sessionName]);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setOpen(false);
        setShowNewInput(false);
        setNewName('');
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  // Focus new-session input when shown
  useEffect(() => {
    if (showNewInput && inputRef.current) {
      inputRef.current.focus();
    }
  }, [showNewInput]);

  const toggleOpen = useCallback(async () => {
    if (open) {
      setOpen(false);
      setShowNewInput(false);
      setNewName('');
      return;
    }
    setOpen(true);
    setError('');
    setLoading(true);
    try {
      const api = window.miniAgent;
      if (api && api.listSessions) {
        const result = await api.listSessions();
        if (result.error) {
          setError(result.error);
        } else {
          setSessions(result.sessions || []);
          setCurrent(result.current || 'default');
        }
      }
    } catch (e) {
      setError('Failed to load sessions');
    } finally {
      setLoading(false);
    }
  }, [open]);

  const handleSelect = useCallback((name) => {
    setOpen(false);
    setShowNewInput(false);
    setNewName('');
    onSwitch(name);
  }, [onSwitch]);

  const handleDelete = useCallback(async (e, name) => {
    e.stopPropagation();
    if (!window.confirm(`Delete session "${name}"? This cannot be undone.`)) return;
    const api = window.miniAgent;
    if (!api || !api.deleteSession) return;
    try {
      const result = await api.deleteSession(name);
      if (result.ok) {
        // Refresh the list
        setSessions((prev) => prev.filter((s) => s !== name));
        // If we deleted current, the backend switches to default — update current
        if (name === current) {
          setCurrent('default');
        }
      } else {
        setError(result.message || 'Delete failed');
      }
    } catch (e) {
      setError('Delete failed');
    }
  }, [current]);

  const handleNewSubmit = useCallback((e) => {
    if (e.key === 'Enter') {
      const name = newName.trim();
      if (!name) return;
      setOpen(false);
      setShowNewInput(false);
      setNewName('');
      onSwitch(name, /* isNew */ true);
    } else if (e.key === 'Escape') {
      setShowNewInput(false);
      setNewName('');
    }
  }, [newName, onSwitch]);

  const handleNewClick = useCallback(() => {
    setShowNewInput(true);
  }, []);

  return (
    <span id="header-session" className="session-picker dim" ref={dropdownRef}>
      <span className="session-label clickable" onClick={toggleOpen} title="Click to manage sessions">
        {current}
      </span>
      {open && (
        <div className="session-dropdown">
          {error && <div className="session-dropdown-error">{error}</div>}
          {loading && <div className="session-dropdown-loading dim">loading…</div>}
          {!loading && !error && sessions.length === 0 && (
            <div className="session-dropdown-empty dim">no sessions</div>
          )}
          {!loading && !error && sessions.map((s) => (
            <div
              key={s}
              className={`session-dropdown-item${s === current ? ' session-current' : ''}`}
              onClick={() => handleSelect(s)}
            >
              {s === current && <span className="session-check">✓ </span>}
              <span className="session-name">{s}</span>
              <button className="session-delete-btn" onClick={(e) => handleDelete(e, s)} title={`Delete "${s}"`} aria-label={`Delete session ${s}`}>×</button>
            </div>
          ))}
          <div className="session-dropdown-divider" />
          {showNewInput ? (
            <div className="session-dropdown-item session-new-input">
              <input
                ref={inputRef}
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={handleNewSubmit}
                placeholder="session name…"
                className="session-new-field"
              />
            </div>
          ) : (
            <div className="session-dropdown-item session-new-item" onClick={handleNewClick}>
              + New session…
            </div>
          )}
        </div>
      )}
    </span>
  );
}
