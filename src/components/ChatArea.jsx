import { useEffect, useRef } from 'react'
import Message from './Message'

export default function ChatArea({ messages, pendingMessages, currentSessionId, onImageOpen }) {
  const bottomRef = useRef(null)
  const chatRef = useRef(null)
  const userScrolledUp = useRef(false)

  const currentPending = pendingMessages
    ? Object.values(pendingMessages).filter(p => p.sessionId === currentSessionId)
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
    if (bottomRef.current && !userScrolledUp.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages.length, currentPending.length])

  return (
    <div id="chat" ref={chatRef}>
      {messages.map((msg, i) => (
        <Message key={i} msg={msg} onImageOpen={onImageOpen} />
      ))}
      {currentPending.map((p, i) => (
        <Message key={'p-' + p.taskId || i} pending={p} onImageOpen={onImageOpen} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
