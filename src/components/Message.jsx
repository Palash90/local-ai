import { useMemo, useRef, useState, useCallback, useEffect } from 'react'
import { marked } from 'marked'
import markedKatex from 'marked-katex-extension'
import DOMPurify from 'dompurify'
import { speak as apiSpeak } from '../api'
import StatusBox from './StatusBox'

marked.use(markedKatex({ throwOnError: false, nonStandard: true }))

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

function CopyButton({ text, genPrompt, imageUrl, forceShow }) {
  const [label, setLabel] = useState('Copy')

  async function handleCopy() {
    setLabel('Copying...')
    try {
      await copyWithImage()
    } catch {
      copyTextOnly()
    }
  }

  async function copyWithImage() {
    let textContent = text || ''
    if (genPrompt) textContent = 'Prompt: ' + genPrompt + '\n\n' + textContent

    if (imageUrl && navigator.clipboard && navigator.clipboard.write) {
      const url = imageUrl.startsWith('http') ? imageUrl : window.location.origin + imageUrl
      try {
        const res = await fetch(url)
        const blob = await res.blob()
        const b64 = await blobToBase64(blob)
        const escaped = textContent.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        const cleanHtml = '<html><body>' + escaped.replace(/\n/g, '<br>') + '<br><br><img src="' + b64 + '"></body></html>'

        await navigator.clipboard.write([
          new ClipboardItem({
            'text/html': new Blob([cleanHtml], { type: 'text/html' }),
            'text/plain': new Blob([escaped + '\n\n' + url], { type: 'text/plain' }),
          }),
          new ClipboardItem({ [blob.type]: blob }),
        ])
        setLabel('Copied!')
        setTimeout(() => setLabel('Copy'), 2000)
        return
      } catch {}
    }

    let copyText = textContent
    if (imageUrl) {
      const url = imageUrl.startsWith('http') ? imageUrl : window.location.origin + imageUrl
      copyText = (copyText ? copyText + '\n\n' : '') + url
    }

    if (copyText && navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(copyText)
      setLabel('Copied!')
      setTimeout(() => setLabel('Copy'), 2000)
      return
    }

    throw new Error('no clipboard method available')
  }

  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onloadend = () => resolve(reader.result)
      reader.onerror = reject
      reader.readAsDataURL(blob)
    })
  }

  function copyTextOnly() {
    let copyText = text || ''
    if (imageUrl) {
      const url = imageUrl.startsWith('http') ? imageUrl : window.location.origin + imageUrl
      copyText = (copyText ? copyText + '\n\n' : '') + url
    }
    if (genPrompt) copyText = 'Prompt: ' + genPrompt + '\n\n' + copyText

    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(copyText).then(() => {
          setLabel('Copied!')
          setTimeout(() => setLabel('Copy'), 2000)
        }).catch(() => fallbackExecCopy(copyText))
      } else {
        fallbackExecCopy(copyText)
      }
    } catch {
      fallbackExecCopy(copyText)
    }
  }

  function fallbackExecCopy(text) {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      setLabel('Copied!')
      setTimeout(() => setLabel('Copy'), 2000)
    } catch {
      setLabel('Failed')
      setTimeout(() => setLabel('Copy'), 2000)
    }
    document.body.removeChild(ta)
  }

  return <button className={'copy-btn' + (forceShow ? ' force-show' : '')} onClick={handleCopy}>{label}</button>
}

let _activeAudio = null

function SpeakButton({ text }) {
  const [speaking, setSpeaking] = useState(false)
  const idRef = useRef(null)

  async function handleClick() {
    const myId = (idRef.current = {})
    if (_activeAudio && _activeAudio._speakId === myId) {
      _activeAudio.pause()
      _activeAudio.currentTime = 0
      _activeAudio = null
      setSpeaking(false)
      return
    }
    if (_activeAudio) {
      _activeAudio.pause()
      _activeAudio.currentTime = 0
      _activeAudio = null
      setSpeaking(false)
    }
    setSpeaking(true)
    try {
      const data = await apiSpeak(text)
      if (idRef.current !== myId) return
      const mime = data.type || 'audio/mpeg'
      const audio = new Audio('data:' + mime + ';base64,' + data.audio)
      audio._speakId = myId
      audio.onended = () => {
        if (_activeAudio === audio) {
          _activeAudio = null
          setSpeaking(false)
        }
      }
      audio.onerror = () => { setSpeaking(false); _activeAudio = null }
      _activeAudio = audio
      audio.play()
    } catch (e) {
      console.warn('TTS error:', e)
      setSpeaking(false)
    }
  }

  return (
    <button
      className={'speak-btn' + (speaking ? ' speaking' : '')}
      onClick={handleClick}
      title={speaking ? 'Stop' : 'Read aloud'}
    >
      {speaking ? '\u23F9' : '\u25B6'}
    </button>
  )
}

const MAX_REASONING = 2000

function ReasoningBlock({ text, open, onToggle }) {
  const preRef = useRef(null)
  const prevLenRef = useRef(0)

  useEffect(() => {
    if (!preRef.current || !text) return
    if (text.length > prevLenRef.current) {
      const el = preRef.current
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      if (atBottom) el.scrollTop = el.scrollHeight
    }
    prevLenRef.current = text.length
  }, [text])

  if (!text) return null
  const capped = text.length > MAX_REASONING ? text.slice(0, MAX_REASONING) + '...' : text

  let html
  try {
    html = DOMPurify.sanitize(marked.parse(capped))
  } catch {
    html = escHtml(capped)
  }

  return (
    <details className="reasoning-block" open={open} onToggle={(e) => onToggle(e.target.open)}>
      <summary>Reasoning</summary>
      <div className="reasoning-text" ref={preRef} dangerouslySetInnerHTML={{ __html: html }} />
    </details>
  )
}

export default function Message({ msg, pending, onImageOpen }) {
  const elRef = useRef(null)
  const chatEl = useRef(null)
  const [popupVisible, setPopupVisible] = useState(null)
  const hideTimer = useRef(null)
  const [reasoningOpen, setReasoningOpen] = useState(false)

  const showPopup = useCallback((idx) => {
    if (hideTimer.current) clearTimeout(hideTimer.current)
    setPopupVisible(idx)
  }, [])

  const hidePopup = useCallback(() => {
    hideTimer.current = setTimeout(() => setPopupVisible(null), 800)
  }, [])

  const role = pending ? 'bot' : msg.role === 'user' ? 'user' : 'bot'

  if (pending) {
    return (
      <>
        {pending._userMsg && <Message msg={pending._userMsg} onImageOpen={onImageOpen} />}
        <div className={`msg bot`} ref={elRef}>
          <div className="msg-content">
            <StatusBox message={pending.message} />
            <ReasoningBlock text={pending.reasoning} open={reasoningOpen} onToggle={setReasoningOpen} />
          </div>
        </div>
      </>
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
  let timestamp = null

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
    if (msg._timestamp) {
      try {
        const d = new Date(msg._timestamp)
        timestamp = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZoneName: 'short' })
      } catch {}
    }
  } else {
    text = typeof msg.content === 'string' ? msg.content : ''
  }

  const ttsText = text
  if (role === 'bot') text = text.replace(/^\s*\[(bn|hi|en)\]\s*/, '')

  const html = useMemo(() => {
    if (!text) return ''
    try {
      return DOMPurify.sanitize(marked.parse(text))
    } catch {
      return escHtml(text)
    }
  }, [text])

  if (role === 'user' && !text && !imageUrl && !userImg && !genPrompt) return null

  return (
    <div className={`msg ${role}`} ref={elRef}>
      {timestamp && <span className="msg-timestamp">{timestamp}</span>}
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
        {role === 'bot' && text && <SpeakButton text={ttsText} />}
        <CopyButton text={text} genPrompt={genPrompt} imageUrl={imageUrl} />
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
      <ReasoningBlock text={msg._reasoning} open={reasoningOpen} onToggle={setReasoningOpen} />
      {text ? (
        <div
          className="msg-content"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ) : !imageUrl && !userImg ? (
        <div className="msg-content empty-response">
          <em>(No response text generated)</em>
        </div>
      ) : null}
    </div>
  )
}
