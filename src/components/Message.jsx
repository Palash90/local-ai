import { useMemo, useRef, useState, useCallback, useEffect, memo } from 'react'
import { marked } from 'marked'
import markedKatex from 'marked-katex-extension'
import DOMPurify from 'dompurify'
import { speak as apiSpeak, getTaskStatus as apiGetTaskStatus, shareMessage as apiShareMessage } from '../api'
import { downloadFile, toApiImage } from '../utils'
import StatusBox from './StatusBox'

marked.use(markedKatex({ throwOnError: false, nonStandard: true }))

// Research-mode citations are stored as `(Author, Venue, Year) [https://url]`
// (the critic parses the square-bracketed URL), but marked would swallow the
// trailing `]` into the link href. Turn `[https://url]` into a proper markdown
// link at render time so no stray bracket shows. Fenced code blocks are
// skipped to avoid corrupting their contents.
const normalizeCitationLinks = (text) => {
  if (!text) return text
  const RE = /\[(https?:\/\/[^\s\]<>]+)\](?!\s*\()/g
  return text
    .split(/(```[\s\S]*?```)/g)
    .map((seg, i) => (i % 2 === 1 ? seg : seg.replace(RE, '[$1]($1)')))
    .join('')
}

const fileLinkExt = {
  name: 'fileLink',
  level: 'inline',
  start(src) {
    const i = src.indexOf('[FILE:')
    return i === -1 ? undefined : i
  },
  tokenizer(src) {
    const match = /^\[FILE:\s*(\S+)\]\(([^)]+)\)/.exec(src)
    if (!match) return undefined
    return { type: 'fileLink', raw: match[0], url: match[1], name: match[2] }
  },
  renderer(token) {
    const url = token.url.replace(/"/g, '&quot;')
    const name = token.name.replace(/"/g, '&quot;')
    return (
      '<a class="file-chip" href="' + url + '" download="' + name + '" title="' + name + '">' +
      '<svg class="file-chip-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">' +
      '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>' +
      '<polyline points="14 2 14 8 20 8"></polyline>' +
      '</svg>' +
      '<span class="file-chip-name">' + name + '</span>' +
      '</a>'
    )
  },
}

marked.use({ extensions: [fileLinkExt] })

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function hostnameFromUrl(u) {
  try {
    return new URL(u).hostname
  } catch {
    return u || ''
  }
}

function formatElapsed(ms) {
  if (ms == null) return ''
  const s = ms / 1000
  if (s < 60) return s.toFixed(1) + 's'
  const m = Math.floor(s / 60)
  const rs = Math.round(s % 60)
  return m + 'm ' + rs + 's'
}

function execCopy(text) {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch { }
  document.body.removeChild(ta)
  return ok
}

async function writeClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text)
    return true
  }
  return execCopy(text)
}

function ShareButton({ sessionId, msgIndex, forceShow }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [share, setShare] = useState(null)

  async function handleShare(e) {
    e.preventDefault()
    e.stopPropagation()
    if (busy || !sessionId || msgIndex == null) return
    setBusy(true)
    setError('')
    try {
      const data = await apiShareMessage(sessionId, msgIndex)
      if (data && data.token) {
        setShare({ url: data.url })
      } else {
        setError((data && data.error) || 'Sharing failed')
      }
    } catch (err) {
      setError(err.message || 'Sharing failed')
    } finally {
      setBusy(false)
    }
  }

  async function copyLink() {
    if (!share) return
    const url = share.url.startsWith('http') ? share.url : window.location.origin + share.url
    await writeClipboard(url)
  }

  return (
    <>
      <button
        className={'share-btn' + (forceShow ? ' force-show' : '')}
        onClick={handleShare}
        title="Share this message"
        aria-label="Share this message"
        disabled={busy}
      >
        {busy ? 'Sharing…' : (
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="18" cy="5" r="3" />
            <circle cx="6" cy="12" r="3" />
            <circle cx="18" cy="19" r="3" />
            <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
            <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
          </svg>
        )}
      </button>
      {share && (
        <div className="share-modal-overlay" onClick={() => setShare(null)}>
          <div className="share-modal" onClick={e => e.stopPropagation()}>
            <div className="share-modal-header">
              <span>Message shared</span>
              <button type="button" className="share-modal-close" onClick={() => setShare(null)}>&#10005;</button>
            </div>
            <p className="share-modal-hint">Anyone on this network with the link can view this message without logging in.</p>
            <input
              readOnly
              className="share-modal-url"
              value={share.url.startsWith('http') ? share.url : window.location.origin + share.url}
              onFocus={e => e.target.select()}
              onKeyDown={e => e.key === 'Enter' && e.target.select()}
            />
            <div className="share-modal-actions">
              <button type="button" className="share-copy-btn" onClick={copyLink}>Copy link</button>
              <a className="share-open-link" href={share.url} target="_blank" rel="noreferrer">Open</a>
            </div>
          </div>
        </div>
      )}
      {error && <span className="share-error" role="alert">{error}</span>}
    </>
  )
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
        const escaped = textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
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
      } catch { }
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
      aria-label={speaking ? 'Stop' : 'Read aloud'}
    >
      {speaking ? (
        <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" aria-hidden="true">
          <rect x="6" y="5" width="4" height="14" rx="1" />
          <rect x="14" y="5" width="4" height="14" rx="1" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true">
          <path d="M8 5v14l11-7z" />
        </svg>
      )}
    </button>
  )
}

function ReasoningBlock({ text, open, onToggle }) {
  const preRef = useRef(null)
  const prevLenRef = useRef(0)
  const [copied, setCopied] = useState(false)

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
  const capped = text

  let html
  try {
    html = DOMPurify.sanitize(marked.parse(capped))
  } catch {
    html = escHtml(capped)
  }

  function handleCopy(e) {
    e.preventDefault()
    e.stopPropagation()
    writeClipboard(text).then((ok) => {
      setCopied(ok)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <details className="reasoning-block" open={open} onToggle={(e) => onToggle(e.target.open)}>
      <summary>
        <span className="reasoning-summary-label">Reasoning</span>
        {open && (
          <button type="button" className="reasoning-copy-btn" onClick={handleCopy} title="Copy reasoning">
            {copied ? 'Copied!' : 'Copy'}
          </button>
        )}
      </summary>
      <div className="reasoning-text" ref={preRef} dangerouslySetInnerHTML={{ __html: html }} />
    </details>
  )
}

function PendingMessage({ pending, onImageOpen, onResolved, onLocationNeeded, selectingRef }) {
  const [message, setMessage] = useState(pending.message || 'Thinking...')
  const [reasoning, setReasoning] = useState(pending.reasoning || '')
  const [reasoningOpen, setReasoningOpen] = useState(true)
  const resolvedRef = useRef(false)
  const locationNotifiedRef = useRef(false)

  useEffect(() => {
    const iv = setInterval(async () => {
      if (resolvedRef.current) return
      let st
      try {
        st = await apiGetTaskStatus(pending.taskId)
      } catch {
        return
      }
      if (!st || st.status === 'done' || st.status === 'error' || st.status === 'cancelled' || st.status === 'unknown' || st.status === 'not_found') {
        if (resolvedRef.current) return
        resolvedRef.current = true
        clearInterval(iv)
        onResolved(pending, st)
        return
      }
      if (st.message === 'location_needed') {
        if (!locationNotifiedRef.current) {
          locationNotifiedRef.current = true
          onLocationNeeded(pending.taskId)
        }
        return
      }
      locationNotifiedRef.current = false
      if (selectingRef && selectingRef.current) return
      setMessage(st.message || 'Working...')
      if (st.reasoning) setReasoning(prev => (prev === st.reasoning ? prev : st.reasoning))
    }, 1000)
    return () => clearInterval(iv)
  }, [pending, onResolved, onLocationNeeded, selectingRef])

  return (
    <>
      {pending._userMsg && (
        <Message
          msg={pending._userMsg}
          onImageOpen={onImageOpen}
          selectingRef={selectingRef}
          onResolved={onResolved}
          onLocationNeeded={onLocationNeeded}
        />
      )}
      <div className={`msg bot`}>
        <div className="msg-content">
          <StatusBox message={message} />
          <ReasoningBlock text={reasoning} open={reasoningOpen} onToggle={setReasoningOpen} />
        </div>
      </div>
    </>
  )
}

function Message({ msg, pending, sessionId, msgIndex, hideSpeak, onImageOpen, selectingRef, onResolved, onLocationNeeded, shareToken }) {
  const elRef = useRef(null)
  const chatEl = useRef(null)
  const [popupVisible, setPopupVisible] = useState(null)
  const hideTimer = useRef(null)
  const [reasoningOpen, setReasoningOpen] = useState(!!pending)
  const [pageModal, setPageModal] = useState(null)
  const codeRef = useRef([])

  const showPopup = useCallback((idx) => {
    if (hideTimer.current) clearTimeout(hideTimer.current)
    setPopupVisible(idx)
  }, [])

  const hidePopup = useCallback(() => {
    hideTimer.current = setTimeout(() => setPopupVisible(null), 800)
  }, [])

  const role = pending ? 'bot' : msg.role === 'user' ? 'user' : 'bot';

  let text = ''
  let userImg = null
  let timestamp = null

  if (msg) {
    if (role === 'user') {
      if (typeof msg.content === 'string') {
        text = msg.content
      } else if (Array.isArray(msg.content)) {
        msg.content.forEach(part => {
          if (part.type === 'text') text += part.text
          else if (part.type === 'image_url') {
            const url = part.image_url.url
            if (url.startsWith('data:')) userImg = url.split(',')[1]
            else if (url.startsWith('/uploads/') || url.startsWith('/output/') || /^https?:/.test(url)) userImg = url
          }
        })
      }
      if (msg._timestamp) {
        try {
          const d = new Date(msg._timestamp)
          timestamp = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZoneName: 'short' })
        } catch { }
      }
    } else {
      text = typeof msg.content === 'string' ? msg.content : ''
    }
  }

  // 2. ALWAYS call useMemo before any conditional return statements
  const html = useMemo(() => {
    if (!text) return ''
    try {
      const codeBlocks = []
      const renderer = new marked.Renderer()
      renderer.code = ({ text: codeText, lang }) => {
        const idx = codeBlocks.length
        codeBlocks.push(codeText.replace(/\n$/, ''))
        const code = (codeText.replace(/\n$/, '') + '\n').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
        const langAttr = lang ? ` class="language-${lang.split(/\s/)[0]}"` : ''
        return `<div class="code-block"><button type="button" class="copy-code-btn" data-i="${idx}" title="Copy code">Copy</button><pre><code${langAttr}>${code}</code></pre></div>\n`
      }
      const out = DOMPurify.sanitize(marked.parse(normalizeCitationLinks(text), { renderer }))
      codeRef.current = codeBlocks
      return out
    } catch {
      return escHtml(text)
    }
  }, [text])

  if (pending) {
    return (
      <PendingMessage
        pending={pending}
        onImageOpen={onImageOpen}
        selectingRef={selectingRef}
        onResolved={onResolved}
        onLocationNeeded={onLocationNeeded}
      />
    )
  }

  if (msg.role === 'system' || msg.role === 'tool') return null

  const toolsUsed = msg._tools_used || []
  const searchDetails = msg._search_details || []
  const fetchDetails = searchDetails.filter(d => d && d.tool === 'fetch_page')
  const genPrompt = msg._gen_prompt
  const imageUrl = toApiImage(msg._image_url, shareToken)
  const imageModel = msg._image_model
  const isUserImgUrl = typeof userImg === 'string' && (userImg.startsWith('/') || /^https?:/.test(userImg))
  const userImgSrc = userImg ? (isUserImgUrl ? toApiImage(userImg, shareToken) : 'data:image/jpeg;base64,' + userImg) : null

  if (
    msg.role === 'assistant' &&
    !text &&
    !imageUrl &&
    !userImg &&
    !genPrompt &&
    msg.tool_calls &&
    msg.tool_calls.length > 0
  ) {
    return null
  }

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
          else if (url.startsWith('/uploads/') || url.startsWith('/output/') || /^https?:/.test(url)) userImg = url
        }
      })
    }
    if (msg._timestamp) {
      try {
        const d = new Date(msg._timestamp)
        timestamp = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZoneName: 'short' })
      } catch { }
    }
  } else {
    text = typeof msg.content === 'string' ? msg.content : ''
  }

  const ttsText = text
  if (role === 'bot') text = text.replace(/^\s*\[(bn|hi|en)\]\s*/, '')

  async function handleContentClick(e) {
    const btn = e.target.closest('.copy-code-btn')
    if (!btn) return
    const idx = parseInt(btn.dataset.i, 10)
    const codeText = codeRef.current[idx]
    if (codeText == null) return
    const orig = btn.textContent
    const ok = await writeClipboard(codeText)
    btn.textContent = ok ? 'Copied!' : 'Failed'
    setTimeout(() => { btn.textContent = orig }, 2000)
  }

  if (role === 'user' && !text && !imageUrl && !userImg && !genPrompt) return null

  return (
    <div className={`msg ${role}`} ref={elRef}>
      {timestamp && <span className="msg-timestamp">{timestamp}</span>}
      <div className="msg-header">
        {role === 'user' && msg._research && (
        <span className="tool-badge research" title="This message was sent with the Research toggle on">Research</span>
      )}
      {role === 'bot' && msg._elapsed_ms != null && (
          <span className="msg-elapsed" title="Time from task start to completion">&#9202; {formatElapsed(msg._elapsed_ms)}</span>
        )}
        {role === 'bot' && toolsUsed.length > 0 && (() => {
          let fetchIdx = 0
          return toolsUsed.map((t, i) => {
            const isFetch = t === 'fetch_page'
            const fetchDetail = isFetch ? fetchDetails[fetchIdx++] : null
            return (
              <span
                key={i}
                className={`tool-badge ${t === 'web_search' ? 'search' : t === 'generate_image' ? 'image' : t === 'edit_image' ? 'edit' : t === 'fetch_page' ? 'fetch' : ''}`}
                onMouseEnter={() => t === 'web_search' && searchDetails.length > 0 && showPopup(i)}
                onMouseLeave={hidePopup}
              >
                {t === 'web_search' ? 'Web Search' : t === 'generate_image' ? `Image Gen${imageModel ? ' (' + imageModel + ')' : ''}` : t === 'edit_image' ? 'Edit Image' : t === 'fetch_page' ? (fetchDetail?.url ? 'Fetched Page · ' + hostnameFromUrl(fetchDetail.url) : 'Fetched Page') : t}
                {isFetch && fetchDetail && (
                  <button
                    type="button"
                    className="fetch-info-btn"
                    title="View page details"
                    onClick={e => { e.stopPropagation(); setPageModal(fetchDetail) }}
                  >
                    &#9432;
                  </button>
                )}
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
            )
          })
        })()}
        {role === 'bot' && text && !hideSpeak && <SpeakButton text={ttsText} />}
        <CopyButton text={text} genPrompt={genPrompt} imageUrl={imageUrl} />
        {role === 'bot' && sessionId && msgIndex != null && (
          <ShareButton sessionId={sessionId} msgIndex={msgIndex} />
        )}
      </div>
      {imageUrl && (
        <div className="image-wrap">
          <img
            src={imageUrl}
            style={{ maxWidth: '100%', borderRadius: 10, cursor: 'pointer' }}
            onClick={() => onImageOpen(imageUrl)}
            alt="Generated"
          />
          <div className="img-actions">
            <button type="button" className="img-download-btn" onClick={() => downloadFile(imageUrl, 'image.png')}>
              Download
            </button>
          </div>
        </div>
      )}
      {userImgSrc && (
        <img
          src={userImgSrc}
          style={{ maxWidth: '100%', borderRadius: 10, cursor: 'pointer' }}
          onClick={() => onImageOpen(userImgSrc)}
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
          onClick={handleContentClick}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ) : !imageUrl && !userImg ? (
        <div className="msg-content empty-response">
          <em>(No response text generated)</em>
        </div>
      ) : null}
      {pageModal && (
        <div className="page-modal-overlay" onClick={() => setPageModal(null)}>
          <div className="page-modal" onClick={e => e.stopPropagation()}>
            <div className="page-modal-header">
              <span>Fetched Page</span>
              <button type="button" className="page-modal-close" onClick={() => setPageModal(null)}>&#10005;</button>
            </div>
            {pageModal.error ? (
              <div className="page-modal-body error">
                <div className="page-modal-url">{pageModal.url}</div>
                <p className="page-modal-error-msg">{pageModal.error}</p>
              </div>
            ) : (
              <div className="page-modal-body">
                {pageModal.title && <div className="page-modal-title">{pageModal.title}</div>}
                <a className="page-modal-url" href={pageModal.url} target="_blank" rel="noreferrer">{pageModal.url}</a>
                <div className="page-modal-content">
                  {(pageModal.content || '(No readable text content extracted)').slice(0, 6000)}
                  {pageModal.content && pageModal.content.length > 6000 ? '\n...[truncated for display]' : ''}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default memo(Message)
