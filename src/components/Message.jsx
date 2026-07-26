import { useMemo, useRef, useState, useCallback, useEffect } from 'react'
import { marked } from 'marked'
import StatusBox from './StatusBox'

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function SearchPopup({ details }) {
  return (
    <div className="search-popup">
      <div style={{ marginBottom: 6 }}>
        <span style={{ color: '#888', fontSize: 11 }}>Query:</span>{' '}
        <span style={{ color: '#e0e0e0' }}>
          {details.map(sd => sd.query).join('; ')}
        </span>
      </div>
      {details.map((sd, i) => (
        <div key={i}>
          {sd.search_url && (
            <div style={{ marginBottom: 6, fontSize: 11, wordBreak: 'break-all' }}>
              <span style={{ color: '#888' }}>SearXNG:</span>{' '}
              <a href={sd.search_url} target="_blank" rel="noreferrer" style={{ color: '#60a5fa' }}>
                {sd.search_url}
              </a>
            </div>
          )}
          {sd.results && sd.results.map((r, j) => (
            <a
              key={j}
              href={r.url}
              target="_blank"
              rel="noreferrer"
              style={{ display: 'block', color: '#60a5fa', fontSize: 12, padding: '2px 0', textDecoration: 'none' }}
              title={r.url}
            >
              {r.title || r.url}
            </a>
          ))}
        </div>
      ))}
    </div>
  )
}

function CopyButton({ text, genPrompt }) {
  const [label, setLabel] = useState('Copy')

  function handleCopy() {
    let copyText = text || ''
    if (genPrompt) copyText = 'Prompt: ' + genPrompt + '\n\n' + copyText
    navigator.clipboard.writeText(copyText).then(() => {
      setLabel('Copied!')
      setTimeout(() => setLabel('Copy'), 2000)
    })
  }

  return <button className="copy-btn" onClick={handleCopy}>{label}</button>
}

export default function Message({ msg, pending, onImageOpen }) {
  const elRef = useRef(null)
  const chatEl = useRef(null)
  const [popupVisible, setPopupVisible] = useState(null)
  const hideTimer = useRef(null)

  useEffect(() => {
    if (elRef.current) {
      const chat = elRef.current.closest('#chat')
      if (chat) chat.scrollTop = chat.scrollHeight
    }
  }, [])

  const showPopup = useCallback((idx) => {
    if (hideTimer.current) clearTimeout(hideTimer.current)
    setPopupVisible(idx)
  }, [])

  const hidePopup = useCallback(() => {
    hideTimer.current = setTimeout(() => setPopupVisible(null), 300)
  }, [])

  const role = pending ? 'bot' : msg.role === 'user' ? 'user' : 'bot'

  const MAX_REASONING = 2000

  function ReasoningBlock({ text, autoOpen }) {
    const ref = useRef(null)
    useEffect(() => {
      if (ref.current) ref.current.open = autoOpen
    }, [])
    if (!text) return null
    const capped = text.length > MAX_REASONING ? text.slice(0, MAX_REASONING) + '...' : text
    return (
      <details className="reasoning-block" ref={ref}>
        <summary>Reasoning</summary>
        <pre className="reasoning-text">{capped}</pre>
      </details>
    )
  }

  if (pending) {
    return (
      <div className={`msg bot`} ref={elRef}>
        <div className="msg-content">
          <StatusBox message={pending.message} />
          <ReasoningBlock text={pending.reasoning} autoOpen={true} />
        </div>
      </div>
    )
  }

  if (msg.role === 'system' || msg.role === 'tool') return null

  const toolsUsed = msg._tools_used || []
  const searchDetails = msg._search_details || []
  const genPrompt = msg._gen_prompt
  const imageUrl = msg._image_url
  const imageModel = msg._image_model

  let text = ''
  let userImg = null

  if (role === 'user') {
    if (typeof msg.content === 'string') {
      text = msg.content
      // @ts-ignore
    } else if (Array.isArray(msg.content)) {
      msg.content.forEach(part => {
        if (part.type === 'text') text += part.text
        else if (part.type === 'image_url') {
          const url = part.image_url.url
          if (url.startsWith('data:')) userImg = url.split(',')[1]
        }
      })
    }
  } else {
    text = typeof msg.content === 'string' ? msg.content : ''
  }

  const html = useMemo(() => {
    if (!text) return ''
    if (text.indexOf('class="status-box"') !== -1) return text
    return marked.parse(text)
  }, [text])

  if (!text && !imageUrl && !userImg && !genPrompt) return null

  return (
    <div className={`msg ${role}`} ref={elRef}>
      <div className="msg-header">
        {role === 'bot' && toolsUsed.length > 0 && toolsUsed.map((t, i) => (
          <span
            key={i}
            className={`tool-badge ${t === 'web_search' ? 'search' : t === 'generate_image' ? 'image' : t === 'edit_image' ? 'edit' : ''}`}
            onMouseEnter={() => t === 'web_search' && searchDetails.length > 0 && showPopup(i)}
            onMouseLeave={hidePopup}
          >
            {t === 'web_search' ? 'Web Search' : t === 'generate_image' ? `Image Gen${imageModel ? ' (' + imageModel + ')' : ''}` : t === 'edit_image' ? 'Edit Image' : t}
            {t === 'web_search' && searchDetails.length > 0 && popupVisible === i && (
              <div
                className="search-popup"
                onMouseEnter={() => showPopup(i)}
                onMouseLeave={hidePopup}
              >
                <SearchPopup details={searchDetails} />
              </div>
            )}
          </span>
        ))}
        <CopyButton text={text} genPrompt={genPrompt} />
      </div>
      {imageUrl && (
        <img
          src={imageUrl}
          style={{ maxWidth: '100%', borderRadius: 10, cursor: 'pointer' }}
          onClick={() => onImageOpen(imageUrl)}
          alt="Generated"
        />
      )}
      {userImg && (
        <img
          src={'data:image/jpeg;base64,' + userImg}
          style={{ maxWidth: '100%', borderRadius: 10, cursor: 'pointer' }}
          onClick={() => onImageOpen('data:image/jpeg;base64,' + userImg)}
          alt="Uploaded"
        />
      )}
      {genPrompt && (
        <div style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: '#888', marginBottom: 6, fontStyle: 'italic' }}>
          Prompt: {genPrompt}
        </div>
      )}
      <ReasoningBlock text={msg._reasoning} autoOpen={false} />
      {text && (
        <div
          className="msg-content"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}
    </div>
  )
}
