import { useState } from 'react'

export default function LoginScreen({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(false)

  async function handleSubmit(e) {
    if (e) {
      e.preventDefault()
    }
    console.log('[LoginScreen] handleSubmit called', { username, password: password ? '***' : '' })
    if (!username || !password) {
      console.log('[LoginScreen] empty fields, returning')
      return
    }
    setError(false)
    try {
      console.log('[LoginScreen] calling onLogin...')
      await onLogin(username, password)
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
      </div>
    </div>
  )
}
