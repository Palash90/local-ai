import { useEffect, useRef } from 'react'
import Message from './Message'

export default function ChatArea({ messages, pendingMessages, currentSessionId, onImageOpen }) {
  const bottomRef = useRef(null)

  const currentPending = pendingMessages
    ? Object.values(pendingMessages).filter(p => p.sessionId === currentSessionId)
    : []

  const allMessages = [...messages, ...currentPending.map(p => ({ _pending: true, ...p }))]

  useEffect(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages.length, currentPending.length])

  return (
    <div id="chat">
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
