import { memo } from 'react';

/**
 * Renders streaming text as a single text node (no per-character spans).
 */
interface CharStreamProps {
  text?: string;
  className?: string;
}

const CharStream = memo(function CharStream({ text, className = '' }: CharStreamProps) {
  return <span className={className}>{text}</span>;
});

export default CharStream;
