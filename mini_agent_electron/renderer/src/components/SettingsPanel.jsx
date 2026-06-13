/**
 * SettingsPanel.jsx -- First-run API key configuration.
 *
 * Shows when no LLM API key is detected.  User picks a provider and
 * enters their key, which is saved to ~/.mini_agent_env and the backend
 * is restarted with it.
 */
import { useState, useCallback } from 'react';

const PROVIDERS = [
  { value: 'deepseek', label: 'DeepSeek',    keyEnv: 'DEEPSEEK_API_KEY' },
  { value: 'claude',   label: 'Claude (Anthropic)', keyEnv: 'CLAUDE_API_KEY' },
  { value: 'xai',      label: 'xAI (Grok)',        keyEnv: 'XAI_API_KEY' },
  { value: 'ollama',   label: 'Ollama (local)',    keyEnv: 'OLLAMA_API_KEY' },
];

export default function SettingsPanel({ onSaved }) {
  const [provider, setProvider] = useState('deepseek');
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const selected = PROVIDERS.find((p) => p.value === provider);
  const needsKey = provider !== 'ollama'; // Ollama runs locally, no key needed

  const handleSave = useCallback(async () => {
    setError('');

    if (needsKey && !apiKey.trim()) {
      setError('API key is required.');
      return;
    }

    setSaving(true);
    try {
      await window.miniAgent.saveApiKey(provider, apiKey.trim());
      await window.miniAgent.restartBackend();
      // The backend:status event (ready:true) will trigger onSaved
    } catch (e) {
      setError(e.message || 'Failed to save. Try again.');
      setSaving(false);
    }
  }, [provider, apiKey, needsKey]);

  const handleKeyDown = useCallback(
    (e) => {
      if (e.key === 'Enter') handleSave();
    },
    [handleSave],
  );

  return (
    <div id="settings-overlay">
      <div id="settings-panel">
        <div id="settings-header">
          <span className="settings-title">mini_agent -- Setup</span>
          <span className="settings-subtitle dim">
            Enter your API key to get started
          </span>
        </div>

        <div id="settings-body">
          {/* Provider selector */}
          <div className="settings-field">
            <label className="settings-label">Provider</label>
            <div className="settings-provider-select">
              {PROVIDERS.map((p) => (
                <button
                  key={p.value}
                  className={`provider-btn${provider === p.value ? ' active' : ''}`}
                  onClick={() => setProvider(p.value)}
                  type="button"
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          {/* API key input -- hidden for Ollama */}
          {needsKey && (
            <div className="settings-field">
              <label className="settings-label" htmlFor="api-key-input">
                {selected?.keyEnv || 'API Key'}
              </label>
              <div className="settings-key-row">
                <input
                  id="api-key-input"
                  className="settings-input"
                  type={showKey ? 'text' : 'password'}
                  placeholder="sk-..."
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  onKeyDown={handleKeyDown}
                  autoFocus
                  spellCheck={false}
                  autoComplete="off"
                />
                <button
                  className="settings-toggle-btn"
                  onClick={() => setShowKey((v) => !v)}
                  type="button"
                  title={showKey ? 'Hide key' : 'Show key'}
                >
                  {showKey ? '\u25C9' : '\u25CE'}
                </button>
              </div>
            </div>
          )}

          {provider === 'ollama' && (
            <div className="settings-ollama-note dim">
              Ollama connects locally -- no API key needed.
              Make sure the Ollama server is running.
            </div>
          )}

          {/* Error */}
          {error && <div className="settings-error">{error}</div>}

          {/* Save button */}
          <button
            id="settings-save-btn"
            onClick={handleSave}
            disabled={saving}
            type="button"
          >
            {saving ? 'Starting...' : 'Save & Start'}
          </button>
        </div>
      </div>
    </div>
  );
}
