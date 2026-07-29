import { useState, useEffect, useRef } from 'react'

export default function ModelBar({ modelStatus, modelTps, tokenEstimate, maxContext, onToggleSidebar, username, onLogout, onCompact, compacting, reminderCount, onToggleTasks }) {
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const dropdownRef = useRef(null)

  useEffect(() => {
    function handleClick(e) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setDropdownOpen(false)
      }
    }
    document.addEventListener('click', handleClick)
    return () => document.removeEventListener('click', handleClick)
  }, [])

  const labels = {
    chat_loaded: 'Chat model ready',
    image_active: 'Image generation active',
    loading: 'Loading model...',
    unloading: 'Unloading model...',
    unloaded: 'No model loaded',
  }

  return (
    <div id="model-bar">
      <button id="sidebar-toggle" onClick={onToggleSidebar}>&#9776;</button>
      <span id="model-dot" className={modelStatus}></span>
      <span id="model-label">{labels[modelStatus] || modelStatus}</span>
      {modelTps != null && (
        <span id="model-tps" style={{ marginLeft: 12, fontSize: 12, color: '#888' }}>
          {modelTps < 5 ? (
            <span style={{ color: '#f87171' }}>&#9888; {modelTps.toFixed(1)} t/s</span>
          ) : (
            <>{modelTps.toFixed(1)} t/s</>
          )}
        </span>
      )}
      <span id="token-indicator">
        <svg id="context-donut" viewBox="0 0 24 24" width="18" height="18">
          <circle cx="12" cy="12" r="8" fill="none" stroke="rgba(255,255,255,0.1)" strokeWidth="3" />
          <circle
            cx="12" cy="12" r="8" fill="none"
            stroke={(tokenEstimate / maxContext) * 100 > 80 ? '#f87171' : (tokenEstimate / maxContext) * 100 > 60 ? '#fbbf24' : '#4ade80'}
            strokeWidth="3" strokeLinecap="round"
            strokeDasharray={Math.PI * 16}
            strokeDashoffset={Math.PI * 16 * (1 - Math.min((tokenEstimate / maxContext) * 100, 100) / 100)}
            transform="rotate(-90 12 12)"
          />
        </svg>
        {tokenEstimate > 0 && (
          <span className="token-text">
            {tokenEstimate > 1000 ? (tokenEstimate / 1000).toFixed(1) + 'k' : tokenEstimate} / {maxContext > 1000 ? (maxContext / 1000).toFixed(0) + 'k' : maxContext}
          </span>
        )}
        {onCompact && (
          <button id="compact-btn" onClick={onCompact} disabled={compacting} title="Compress old messages to free context">
            {compacting ? '...' : '\u21911'}
          </button>
        )}
        <button id="tasks-btn" onClick={onToggleTasks} title="Tasks">
          &#9776;{reminderCount > 0 && <span id="reminder-badge">{reminderCount}</span>}
        </button>
      </span>
      <div id="user-menu" ref={dropdownRef}>
        <span id="user-name" onClick={() => setDropdownOpen(o => !o)}>{username}</span>
        <div id="user-dropdown" className={dropdownOpen ? 'open' : ''}>
          <button onClick={onLogout}>Logout</button>
        </div>
      </div>
    </div>
  )
}
