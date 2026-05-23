/**
 * Rounded frame wrapper for panels — renders ASCII border characters
 * (╭─╮, ╰─╯) with a title in the top bar.
 */
export default function RoundedFrame({ id, title, children }) {
  return (
    <div id={id} className="panel rounded-frame">
      <div className="frame-top">
        <span className="border-char">╭</span>
        <span className="frame-title"> {title} </span>
        <span className="border-char border-fill">─</span>
        <span className="border-char">╮</span>
      </div>
      <div className="frame-body">
        <div className="frame-left"></div>
        <div className="frame-content">{children}</div>
        <div className="frame-right"></div>
      </div>
      <div className="frame-bottom">
        <span className="border-char">╰</span>
        <span className="border-char border-fill">─</span>
        <span className="border-char">╯</span>
      </div>
    </div>
  );
}
