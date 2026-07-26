export default function Sidebar({
  sessions,
  currentSessionId,
  onSwitchSession,
  onNewChat,
  onRenameSession,
  onDeleteSession,
  onClose,
  open,
}) {
  return (
    <>
      <div id="sidebar-overlay" className={open ? 'open' : ''} onClick={onClose}></div>
      <div id="sidebar" className={open ? 'open' : ''}>
        <div id="sidebar-header">
          <button id="new-chat-btn" onClick={onNewChat}>+ New Chat</button>
        </div>
        <div id="session-list">
          {sessions.map(s => (
            <div
              key={s.session_id}
              className={`session-item${s.session_id === currentSessionId ? ' active' : ''}`}
              onClick={() => onSwitchSession(s.session_id)}
            >
              <span className="session-name">
                {s.name.length > 30 ? s.name.slice(0, 30) + '...' : s.name}
              </span>
              <span className="session-actions">
                <button
                  title="Rename"
                  onClick={e => { e.stopPropagation(); onRenameSession(s.session_id) }}
                  dangerouslySetInnerHTML={{ __html: '&#9998;' }}
                />
                <button
                  title="Delete"
                  onClick={e => { e.stopPropagation(); onDeleteSession(s.session_id) }}
                  dangerouslySetInnerHTML={{ __html: '&#128465;' }}
                />
              </span>
            </div>
          ))}
        </div>
      </div>
    </>
  )
}
