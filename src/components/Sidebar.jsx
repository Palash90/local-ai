import { useState } from 'react'

export default function Sidebar({
  sessions,
  currentSessionId,
  onSwitchSession,
  onNewChat,
  onRenameSession,
  onDeleteSession,
  onClose,
  open,
  shares,
  showShares,
  onToggleShares,
  onRevokeShare,
}) {
  const [copiedToken, setCopiedToken] = useState(null)

  const copyLink = (e, s) => {
    e.stopPropagation()
    navigator.clipboard
      .writeText(window.location.origin + s.url)
      .then(() => {
        setCopiedToken(s.token)
        setTimeout(() => setCopiedToken(null), 1500)
      })
  }

  return (
    <>
      <div id="sidebar-overlay" className={open ? 'open' : ''} onClick={onClose}></div>
      <div id="sidebar" className={open ? 'open' : ''}>
        <div id="sidebar-header">
          {showShares ? (
            <button id="new-chat-btn" onClick={onToggleShares}>← Back to Chats</button>
          ) : (
            <button id="new-chat-btn" onClick={onNewChat}>+ New Chat</button>
          )}
          <button
            className="sidebar-tab"
            title="Shared messages"
            onClick={onToggleShares}
          >
            {showShares ? '⇪' : '⇧'}
          </button>
        </div>
        {showShares ? (
          <div id="session-list">
            {shares.length === 0 && (
              <div className="session-item"><span className="session-name">No shared messages</span></div>
            )}
            {shares.map(s => (
              <div key={s.token} className="session-item" onClick={() => window.open(s.url, '_blank')}>
                <span className="session-name">
                  {(s.preview || '(image only)').slice(0, 34)}
                  {!s.session_exists && <em className="share-orphan"> · chat deleted</em>}
                </span>
                <span className="session-actions">
                  <button
                    title={copiedToken === s.token ? 'Copied!' : 'Copy link'}
                    onClick={e => copyLink(e, s)}
                    dangerouslySetInnerHTML={{ __html: copiedToken === s.token ? '&#10003;' : '&#128279;' }}
                  />
                  <button
                    title={s.session_exists ? 'Stop sharing' : 'Unshare and delete orphaned files'}
                    onClick={e => { e.stopPropagation(); onRevokeShare(s) }}
                    dangerouslySetInnerHTML={{ __html: '&#128465;' }}
                  />
                </span>
              </div>
            ))}
          </div>
        ) : (
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
        )}
      </div>
    </>
  )
}
