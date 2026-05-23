/**
 * Rounded frame wrapper for panels — uses CSS border-radius styling.
 * (The old ASCII border characters ╭─╮ / ╰─╯ were hidden by CSS and
 *  are now removed entirely.)
 */
export default function RoundedFrame({ id, title, children }) {
  return (
    <div id={id} className="panel rounded-frame">
      <div className="frame-body">
        <div className="frame-content">{children}</div>
      </div>
    </div>
  );
}
