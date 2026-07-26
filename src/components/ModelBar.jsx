import { useState, useEffect, useRef } from 'react'

export default function ModelBar({ modelStatus, modelTps, tokenEstimate, onToggleSidebar, username, onLogout }) {
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
      <span id="token-indicator" style={{ marginLeft: 12, fontSize: 12, color: '#666' }}>
        {tokenEstimate > 1000 ? '~' + (tokenEstimate / 1000).toFixed(1) + 'k tokens' : tokenEstimate > 0 ? '~' + tokenEstimate + ' tokens' : ''}
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
