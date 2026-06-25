/**
 * Rounded frame wrapper for panels -- uses CSS border-radius styling.
 */
interface RoundedFrameProps {
  id?: string;
  children?: React.ReactNode;
  className?: string;
}

export default function RoundedFrame({ id, children, className }: RoundedFrameProps) {
  return (
    <div id={id} className={`panel rounded-frame${className ? ` ${className}` : ''}`}>
      <div className="frame-body">
        <div className="frame-content">{children}</div>
      </div>
    </div>
  );
}
