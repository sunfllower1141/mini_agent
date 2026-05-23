import { Component } from 'react';

/**
 * Catches render errors in the tree so the UI doesn't whitescreen.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error('[ErrorBoundary]', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{
          padding: '2rem',
          color: 'var(--red)',
          fontFamily: 'var(--font-family)',
          fontSize: 'var(--font-size)',
        }}>
          <h3 style={{ marginBottom: '1rem' }}>Something broke</h3>
          <pre style={{ whiteSpace: 'pre-wrap', color: 'var(--dim)' }}>
            {this.state.error.message}
          </pre>
          <button
            onClick={() => this.setState({ error: null })}
            style={{
              marginTop: '1rem',
              padding: '6px 16px',
              background: 'var(--accent)',
              color: 'var(--bg)',
              border: 'none',
              borderRadius: 4,
              cursor: 'pointer',
              fontFamily: 'var(--font-family)',
            }}
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
