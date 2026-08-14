import { useState } from 'react'

function extractToken(value) {
  const trimmed = value.trim()
  const m = /\/s\/([A-Za-z0-9]+)\/?/.exec(trimmed)
  if (m) return m[1]
  if (/^[A-Za-z0-9]{8,}$/.test(trimmed)) return trimmed
  return ''
}

export default function LoginScreen({ onLogin, onOpenShare }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(false)
  const [shareToken, setShareToken] = useState('')

  async function handleSubmit(e) {
    if (e) {
      e.preventDefault()
    }
    const user = username.trim()
    const pass = password.trim()
    console.log('[LoginScreen] handleSubmit called', { username: user, password: pass ? '***' : '' })
    if (!user || !pass) {
      console.log('[LoginScreen] empty fields, returning')
      return
    }
    setError(false)
    try {
      console.log('[LoginScreen] calling onLogin...')
      await onLogin(user, pass)
      console.log('[LoginScreen] onLogin completed without error')
    } catch (e) {
      console.log('[LoginScreen] onLogin threw:', e.message)
      setError(true)
    }
    console.log('[LoginScreen] handleSubmit finished')
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter') handleSubmit(e)
  }

  function handleOpenShare(e) {
    e.preventDefault()
    const token = extractToken(shareToken)
    if (!token) return
    if (onOpenShare) {
      onOpenShare(token)
    } else {
      window.location.href = '/s/' + token
    }
  }

  return (
    <div id="login-overlay">
      <div className="login-box">
        <h2>Local AI</h2>
        <input
          type="text"
          placeholder="Username"
          autoComplete="username"
          value={username}
          onChange={e => setUsername(e.target.value)}
          onKeyDown={handleKeyDown}
        />
        <input
          type="password"
          placeholder="Password"
          autoComplete="current-password"
          value={password}
          onChange={e => setPassword(e.target.value)}
          onKeyDown={handleKeyDown}
        />
        <button onClick={handleSubmit}>Sign In</button>
        <div className={`login-error${error ? ' show' : ''}`}>Invalid username or password</div>
        <div className="login-share-divider" />
        <input
          type="text"
          className="login-share-input"
          placeholder="Or paste a shared message link"
          aria-label="Shared message link"
          value={shareToken}
          onChange={e => setShareToken(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') handleOpenShare(e) }}
        />
        <button className="login-share-btn" onClick={handleOpenShare}>View shared message</button>
      </div>
    </div>
  )
}
