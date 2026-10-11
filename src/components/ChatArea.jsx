import { useEffect, useRef } from 'react'
import Message from './Message'

export default function ChatArea({ messages, pendingMessages, failedSends, currentSessionId, onImageOpen, selectingRef, onPendingResolved, onLocationNeeded, onResendFailed, onDismissFailed }) {
  const bottomRef = useRef(null)
  const chatRef = useRef(null)
  const userScrolledUp = useRef(false)

  const currentPending = pendingMessages
    ? Object.values(pendingMessages).filter(p => p.sessionId === currentSessionId)
    : []
  const currentFailed = failedSends
    ? Object.entries(failedSends).filter(([, f]) => f.sessionId === currentSessionId)
    : []

  const allMessages = [...messages, ...currentPending.map(p => ({ _pending: true, ...p }))]

  useEffect(() => {
    const el = chatRef.current
    if (!el) return
    const handler = () => {
      const threshold = 100
      userScrolledUp.current = el.scrollHeight - el.scrollTop - el.clientHeight > threshold
    }
    el.addEventListener('scroll', handler, { passive: true })
    return () => el.removeEventListener('scroll', handler)
  }, [])

  useEffect(() => {
    const el = chatRef.current
    if (!el || !selectingRef) return
    const down = () => { selectingRef.current = true }
    const up = () => { selectingRef.current = false }
    el.addEventListener('pointerdown', down)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
    el.addEventListener('touchend', up)
    window.addEventListener('mouseup', up)
    return () => {
      el.removeEventListener('pointerdown', down)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
      el.removeEventListener('touchend', up)
      window.removeEventListener('mouseup', up)
    }
  }, [selectingRef])

  useEffect(() => {
    if (!selectingRef) return
    const onSelChange = () => {
      let active = false
      try { active = !!document.getSelection() && !document.getSelection().isCollapsed } catch { }
      selectingRef.current = active
    }
    document.addEventListener('selectionchange', onSelChange)
    return () => document.removeEventListener('selectionchange', onSelChange)
  }, [selectingRef])

  useEffect(() => {
    if (bottomRef.current && !userScrolledUp.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages.length, currentPending.length, currentFailed.length])

  return (
    <div id="chat" ref={chatRef}>
      {messages.map((msg, i) => (
        <Message
          key={i}
          msg={msg}
          sessionId={currentSessionId}
          msgIndex={i}
          onImageOpen={onImageOpen}
          selectingRef={selectingRef}
          onResolved={onPendingResolved}
          onLocationNeeded={onLocationNeeded}
        />
      ))}
      {currentPending.map((p, i) => (
        <Message
          key={'p-' + (p.taskId || i)}
          pending={p}
          onImageOpen={onImageOpen}
          selectingRef={selectingRef}
          onResolved={onPendingResolved}
          onLocationNeeded={onLocationNeeded}
        />
      ))}
      {currentFailed.map(([key, f]) => (
        <div className="msg user" key={'f-' + key}>
          <div className="msg-content">
            <div className="failed-send-text">{f.text || '(message text unavailable)'}</div>
            <div className="failed-send-error">⚠ Not sent: {f.error}</div>
            <div className="failed-send-actions">
              {f.text ? <button type="button" onClick={() => onResendFailed && onResendFailed(key)}>Resend</button> : null}
              <button type="button" onClick={() => onDismissFailed && onDismissFailed(key)}>Dismiss</button>
            </div>
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
