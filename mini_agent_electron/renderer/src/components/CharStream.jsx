/**
 * Character-level fade-in: renders text as spans, new chars animate in.
 */
export default function CharStream({ text, className = '' }) {
  return (
    <span className={className}>
      {[...text].map((ch, i) => (
        <span key={i} className="stream-char">{ch}</span>
      ))}
    </span>
  );
}
