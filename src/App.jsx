import { useState, useEffect, useRef } from 'react'
import * as api from './api'
import LoginScreen from './components/LoginScreen'
import ModelBar from './components/ModelBar'
import Sidebar from './components/Sidebar'
import ChatArea from './components/ChatArea'
import InputBar from './components/InputBar'
import ImageLightbox from './components/ImageLightbox'
import OverloadWarning from './components/OverloadWarning'

export default function App() {
  const [authenticated, setAuthenticated] = useState(false)
  const [username, setUsername] = useState('')
  const [sessions, setSessions] = useState([])
  const [currentSessionId, setCurrentSessionId] = useState(null)
  const [messages, setMessages] = useState([])
  const [pendingMessages, setPendingMessages] = useState({})
  const [tokenEstimate, setTokenEstimate] = useState(0)
  const [modelStatus, setModelStatus] = useState('unloaded')
  const [modelTps, setModelTps] = useState(null)
  const [overheated, setOverheated] = useState(false)
  const [gpuTemp, setGpuTemp] = useState(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [lightboxSrc, setLightboxSrc] = useState(null)
  const [micRecording, setMicRecording] = useState(false)
  const [loadingSessions, setLoadingSessions] = useState({})
  const mediaRecorderRef = useRef(null)
  const audioChunksRef = useRef([])
  const pendingRef = useRef({})


  useEffect(() => {
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        pos => api.sendLocation(pos.coords.latitude, pos.coords.longitude),
        () => {},
        { timeout: 10000 }
      )
    }
  }, [])

  useEffect(() => {
  const token = localStorage.getItem('auth_token') // or whatever key api.login uses
  if (token) {
    api.checkAuth()
      .then(res => {
        if (res.authenticated) {
          setAuthenticated(true)
          if (res.username) setUsername(res.username)
          loadSessions().then(list => {
            const lastSid = localStorage.getItem('last_sid')
            if (lastSid && list.some(s => s.session_id === lastSid)) {
              switchSession(lastSid)
            } else if (list.length > 0) {
              switchSession(list[0].session_id)
            }
          })
        } else {
          localStorage.removeItem('auth_token')
        }
      })
      .catch(() => setAuthenticated(false))
  }
}, [])

  useEffect(() => {
    pendingRef.current = pendingMessages
  }, [pendingMessages])

  useEffect(() => {
    const handler = () => {
      setAuthenticated(false);
      setUsername('');
      setSessions([]);
      setCurrentSessionId(null);
      setMessages([]);
      setPendingMessages({});
      setSidebarOpen(false);
    };
    window.addEventListener('auth:unauthorized', handler);
    return () => window.removeEventListener('auth:unauthorized', handler);
  }, [])

  const hasPendingForCurrent = Object.values(pendingMessages).some(
    p => p.sessionId === currentSessionId
  )

  // ---- Auth ----
  async function handleLogin(username, password) {
    console.log('[login] attempting login for', username)
    let data
    try {
      data = await api.login(username, password)
      console.log('[login] server response', JSON.stringify(data))
    } catch (e) {
      console.log('[login] api.login threw:', e.message)
      throw e
    }
    if (data && data.token) {
      console.log('[login] token received, setting authenticated')
      setAuthenticated(true)
      if (data.username) setUsername(data.username)
      console.log('[login] state updates queued, returning')
      return
    }
    console.log('[login] no token in response, throwing')
    throw new Error(data ? data.error || 'Login failed' : 'Login failed')
  }

  async function handleLogout() {
    console.log('[logout] called')
    await api.logout()
    setAuthenticated(false)
    setUsername('')
    setSessions([])
    setCurrentSessionId(null)
    setMessages([])
    setPendingMessages({})
    setSidebarOpen(false)
  }

  // ---- Sessions ----
  async function loadSessions() {
    const list = await api.fetchSessions()
    setSessions(list)
    return list
  }

  function loadSessionMessages(sid) {
    api.fetchMessages(sid).then(data => {
      setMessages(data.messages || [])
      setTokenEstimate(data.token_estimate || 0)
    }).catch(() => {
      setMessages([])
      setTokenEstimate(0)
    })
  }

  async function switchSession(sid) {
    setCurrentSessionId(sid)
    localStorage.setItem('last_sid', sid)
    loadSessionMessages(sid)
    closeSidebar()
  }

  async function newChat() {
    const data = await api.createSession()
    setCurrentSessionId(data.session_id)
    localStorage.setItem('last_sid', data.session_id)
    setMessages([])
    setTokenEstimate(0)
    const list = await loadSessions()
    setSessions(list)
    closeSidebar()
  }

  async function deleteSession_(sid) {
    if (!confirm('Delete this session?')) return
    await api.deleteSession(sid)
    const list = await loadSessions()
    if (sid === currentSessionId) {
      if (list.length > 0) {
        switchSession(list[0].session_id)
      } else {
        newChat()
      }
    }
  }

  async function renameSession_(sid) {
    const s = sessions.find(x => x.session_id === sid)
    const newName = prompt('Session name:', s ? s.name : '')
    if (!newName || newName.trim() === '') return
    const prevSid = currentSessionId
    await api.renameSession(sid, newName.trim())
    const list = await loadSessions()
    setCurrentSessionId(prevSid)
    setSessions(list)
  }

  function closeSidebar() {
    setSidebarOpen(false)
  }

  // ---- Chat / Send ----
  async function handleSend(text, image) {
    if (!currentSessionId) return
    const taskSid = currentSessionId
    setLoadingSessions(prev => ({ ...prev, [taskSid]: (prev[taskSid] || 0) + 1 }))

    const userMsg = { role: 'user', content: text || (attachedFileLabel(image)), _timestamp: new Date().toISOString() }

    try {
      const data = await api.sendMessage(currentSessionId, text || '', image || undefined, undefined, new Date().toISOString())
      const taskId = data.task_id

      setPendingMessages(prev => ({
        ...prev,
        [taskId]: { sessionId: taskSid, status: 'working', message: 'Thinking...', taskId, reasoning: '', _userMsg: userMsg },
      }))

      const stored = getStoredPending()
      stored.push({ task_id: taskId, sid: taskSid })
      setStoredPending(stored)
    } catch (err) {
      setMessages(prev => [...prev, userMsg, { role: 'assistant', content: 'Error: ' + err.message }])
    }

    setLoadingSessions(prev => {
      const next = { ...prev }
      next[taskSid] = (next[taskSid] || 1) - 1
      if (!next[taskSid]) delete next[taskSid]
      return next
    })
  }

  function attachedFileLabel(image) {
    return image ? '\uD83D\uDCC4 file' : '\uD83C\uDFA4 Audio message'
  }

  // ---- Audio ----
  async function toggleMic() {
    if (micRecording) {
      stopRecording()
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mr = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' })
      audioChunksRef.current = []
      setMicRecording(true)
      mediaRecorderRef.current = mr

      mr.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunksRef.current.push(e.data)
      }

      mr.onstop = () => {
        setMicRecording(false)
        stream.getTracks().forEach(t => t.stop())
        const blob = new Blob(audioChunksRef.current, { type: 'audio/webm' })
        const reader = new FileReader()
        reader.onloadend = () => {
          const b64 = reader.result.split(',')[1]
          sendAudio(b64)
        }
        reader.readAsDataURL(blob)
      }

      mr.start()
    } catch (e) {
      alert('Microphone access denied: ' + e.message)
    }
  }

  function stopRecording() {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      mediaRecorderRef.current.stop()
    }
  }

  async function sendAudio(audioB64) {
    if (!currentSessionId) return
    const taskSid = currentSessionId
    setLoadingSessions(prev => ({ ...prev, [taskSid]: (prev[taskSid] || 0) + 1 }))
    const userMsg = { role: 'user', content: '\uD83C\uDFA4 Audio message', _timestamp: new Date().toISOString() }

    try {
      const data = await api.sendMessage(currentSessionId, '', undefined, audioB64)
      const taskId = data.task_id
      setPendingMessages(prev => ({
        ...prev,
        [taskId]: { sessionId: taskSid, status: 'working', message: 'Thinking...', taskId, reasoning: '', _userMsg: userMsg },
      }))
      const stored = getStoredPending()
      stored.push({ task_id: taskId, sid: taskSid })
      setStoredPending(stored)
    } catch (err) {
      setMessages(prev => [...prev, userMsg, { role: 'assistant', content: 'Error: ' + err.message }])
    }

    setLoadingSessions(prev => {
      const next = { ...prev }
      next[taskSid] = (next[taskSid] || 1) - 1
      if (!next[taskSid]) delete next[taskSid]
      return next
    })
  }

  // ---- Task polling ----
  useEffect(() => {
    if (!authenticated) return
    const interval = setInterval(async () => {
      const pends = pendingRef.current
      const taskIds = Object.keys(pends)
      if (taskIds.length === 0) return

      for (const taskId of taskIds) {
        try {
          const st = await api.getTaskStatus(taskId)
          if (st.status === 'done' || st.status === 'error') {
            const pendingEntry = pends[taskId]
            setPendingMessages(prev => {
              const next = { ...prev }
              delete next[taskId]
              return next
            })
            if (st.status === 'done') {
              const userMsg = pendingEntry?._userMsg
              const assistantMsg = {
                role: 'assistant',
                content: st.response || '',
                _reasoning: st.reasoning || '',
                _image_url: st.image || st._image_url,
                _gen_prompt: st.gen_prompt,
                _tools_used: st.tools_used || [],
                _image_model: st._image_model,
                _search_details: st._search_details || [],
              }
              setMessages(prev => [...prev, ...(userMsg ? [userMsg] : []), assistantMsg])
              if (st.token_estimate != null) setTokenEstimate(st.token_estimate)
              if (st.predicted_per_second != null) setModelTps(st.predicted_per_second)
            } else {
              setMessages(prev => [...prev, { role: 'assistant', content: 'Error: ' + (st.error || '') }])
            }
            const remaining = getStoredPending().filter(p => p.task_id !== taskId)
            setStoredPending(remaining)
          } else {
            setPendingMessages(prev => ({
              ...prev,
              [taskId]: { ...prev[taskId], status: st.status, message: st.message || 'Working...', reasoning: st.reasoning || prev[taskId]?.reasoning },
            }))
          }
        } catch {
          // ignore polling errors
        }
      }
    }, 1000)
    return () => clearInterval(interval)
  }, [authenticated])

  // ---- Model status polling ----
  useEffect(() => {
    if (!authenticated) return
    const interval = setInterval(async () => {
      try {
        const data = await api.getModelStatus()
        setModelStatus(data.model)
        setOverheated(data.overheated)
        setGpuTemp(data.gpu_temp)
        if (data.predicted_per_second != null) setModelTps(data.predicted_per_second)
      } catch { /* ignore */ }
    }, 2000)
    return () => clearInterval(interval)
  }, [authenticated])

  // ---- Resume pending tasks on page load ----
  useEffect(() => {
    if (!authenticated) return
    const stored = getStoredPending()
    if (stored.length > 0) {
      const pMap = {}
      stored.forEach(item => {
        pMap[item.task_id] = { sessionId: item.sid, status: 'working', message: 'Thinking...', taskId: item.task_id, reasoning: '' }
      })
      setPendingMessages(pMap)
    }
  }, [authenticated])

  // ---- When authenticated, set up sessions ----
  async function handleLoginWrapper(username, password) {
    console.log('[loginWrapper] starting')
    try {
      await handleLogin(username, password)
      console.log('[loginWrapper] after handleLogin, authenticated should be true')
      if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(
          pos => api.sendLocation(pos.coords.latitude, pos.coords.longitude),
          () => {},
          { timeout: 10000 }
        )
      }
      console.log('[loginWrapper] loading sessions...')
      const list = await loadSessions()
      console.log('[loginWrapper] sessions loaded', list.length)
      const lastSid = localStorage.getItem('last_sid')
      if (lastSid && list.some(s => s.session_id === lastSid)) {
        switchSession(lastSid)
      } else if (list.length > 0) {
        switchSession(list[0].session_id)
      } else {
        newChat()
      }
      console.log('[loginWrapper] done')
    } catch (err) {
      console.error('[loginWrapper] session load failed', err)
      throw err
    }
  }

  return (
    <>
      <div style={{ display: !authenticated ? '' : 'none' }}>
        <LoginScreen onLogin={handleLoginWrapper} />
      </div>
      <div style={{ display: authenticated ? '' : 'none' }}>
        <ModelBar
          modelStatus={modelStatus}
          modelTps={modelTps}
          tokenEstimate={tokenEstimate}
          onToggleSidebar={() => setSidebarOpen(o => !o)}
          username={username}
          onLogout={handleLogout}
        />
        <div id="app-container">
          <Sidebar
            sessions={sessions}
            currentSessionId={currentSessionId}
            onSwitchSession={switchSession}
            onNewChat={newChat}
            onRenameSession={renameSession_}
            onDeleteSession={deleteSession_}
            onClose={closeSidebar}
            open={sidebarOpen}
          />
          <OverloadWarning overheated={overheated} gpuTemp={gpuTemp} />
          <ChatArea
            messages={messages}
            pendingMessages={pendingMessages}
            currentSessionId={currentSessionId}
            onImageOpen={setLightboxSrc}
          />
          <InputBar
            onSend={handleSend}
            hasPending={hasPendingForCurrent}
            micRecording={micRecording}
            onMicToggle={toggleMic}
          />
        </div>
        <ImageLightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />
      </div>
    </>
  )
}

function getStoredPending() {
  try {
    return JSON.parse(sessionStorage.getItem('opencode_pending')) || []
  } catch {
    return []
  }
}

function setStoredPending(arr) {
  try {
    sessionStorage.setItem('opencode_pending', JSON.stringify(arr))
  } catch { /* ignore */ }
}
