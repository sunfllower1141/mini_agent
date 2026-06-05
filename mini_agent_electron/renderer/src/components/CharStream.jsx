import { memo } from 'react';

/**
 * Renders streaming text as a single text node (no per-character spans).
 * Per-character DOM nodes were burning CPU for zero visual benefit — text
 * streaming at 20 fps already looks smooth, and creating 5000+ span elements
 * on every tick caused React to diff thousands of nodes 20 times/sec.
 */
const CharStream = memo(function CharStream({ text, className = '' }) {
  return <span className={className}>{text}</span>;
});

export default CharStream;
