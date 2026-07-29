import { useState, useRef, useCallback } from 'react'
import { extractFile } from '../api'

const CODE_EXTS = new Set([
  '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.cpp', '.c', '.h', '.hpp',
  '.cs', '.go', '.rs', '.rb', '.php', '.swift', '.kt', '.scala', '.dart',
  '.sh', '.bash', '.pl', '.pm', '.lua', '.r', '.sql', '.html', '.css',
  '.scss', '.sass', '.less', '.vue', '.svelte', '.yaml', '.yml', '.json',
  '.xml', '.toml', '.ini', '.cfg', '.md', '.tex', '.dockerfile', '.tf',
  '.zig', '.nim', '.hs', '.ml', '.fs', '.erl', '.elm', '.purs', '.nix',
  '.ps1', '.bat', '.cmake', '.proto', '.gradle', '.bib',
])

const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'])
const DOC_EXTS = new Set(['.pdf', '.xls', '.xlsx', '.doc', '.docx'])
const MAX_FILE_SIZE = 10 * 1024 * 1024

export default function InputBar({ onSend, hasPending, micRecording, onMicToggle }) {
  const [text, setText] = useState('')
  const [attachedImage, setAttachedImage] = useState(null)
  const [attachedFile, setAttachedFile] = useState(null)
  const [attachedFileText, setAttachedFileText] = useState(null)
  const fileInputRef = useRef(null)
  const textareaRef = useRef(null)
  const imagePreviewRef = useRef(null)
  const sendingRef = useRef(false)

  function clearAttachments() {
    setAttachedImage(null)
    setAttachedFile(null)
    setAttachedFileText(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
    if (imagePreviewRef.current) imagePreviewRef.current.style.display = 'none'
  }

  const handleFile = useCallback(async (e) => {
    const file = e.target.files[0]
    if (!file) return
    if (file.size > MAX_FILE_SIZE) {
      alert('File too large (max 10MB): ' + file.name)
      e.target.value = ''
      return
    }
    clearAttachments()
    const ext = '.' + file.name.split('.').pop().toLowerCase()

    if (IMAGE_EXTS.has(ext)) {
      const reader = new FileReader()
      reader.onload = (ev) => {
        const img = new Image()
        img.onload = () => {
          let w = img.naturalWidth
          let h = img.naturalHeight
          const MAX_DIM = 1920
          if (w > MAX_DIM || h > MAX_DIM) {
            if (w > h) { h = Math.round(h * MAX_DIM / w); w = MAX_DIM }
            else { w = Math.round(w * MAX_DIM / h); h = MAX_DIM }
          }
          const c = document.createElement('canvas')
          c.width = w; c.height = h
          const ctx = c.getContext('2d')
          ctx.drawImage(img, 0, 0, w, h)
          const compressed = c.toDataURL('image/jpeg', 0.8)
          const b64 = compressed.split(',')[1]
          setAttachedImage(b64)
          if (imagePreviewRef.current) {
            imagePreviewRef.current.src = compressed
            imagePreviewRef.current.style.display = 'block'
          }
        }
        img.src = ev.target.result
      }
      reader.readAsDataURL(file)
      return
    }

    if (CODE_EXTS.has(ext)) {
      const text = await file.text()
      if (text.length > 100000) {
        alert('File too large (text exceeds 100KB): ' + file.name)
        return
      }
      setAttachedFile(file.name)
      setAttachedFileText(text)
      return
    }

    if (DOC_EXTS.has(ext)) {
      const reader = new FileReader()
      reader.onload = async (ev) => {
        const b64 = ev.target.result.split(',')[1]
        try {
          const data = await extractFile(file.name, b64)
          if (data.text) {
            setAttachedFile(file.name)
            setAttachedFileText(data.text)
          } else {
            clearAttachments()
            alert('Could not extract text from ' + file.name + (data.error ? ': ' + data.error : ''))
          }
        } catch (err) {
          clearAttachments()
          alert('Error processing file: ' + err.message)
        }
      }
      reader.readAsDataURL(file)
      return
    }

    const isCode = confirm('Unknown file type: ' + file.name + '. Is this a code/text file?')
    if (isCode) {
      const t = await file.text()
      setAttachedFile(file.name)
      setAttachedFileText(t)
    } else {
      clearAttachments()
    }
  }, [])

  async function handleSend() {
    if (sendingRef.current) return
    const msg = text.trim()
    if (!msg && !attachedImage && !attachedFileText) return
    sendingRef.current = true
    let finalText = msg
    if (attachedFileText) {
      finalText = '[FILE: ' + attachedFile + ']\n```\n' + attachedFileText + '\n```\n[END FILE]\n\n' + (msg || 'See attached file above.')
    }
    try {
      await onSend(finalText, attachedImage)
    } finally {
      sendingRef.current = false
    }
    setText('')
    clearAttachments()
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  const isMobile = window.matchMedia('(pointer: coarse)').matches;

  function handleKeyDown(e) {
    if (isMobile) return;
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) {
      e.preventDefault()
      handleSend()
    }
  }

  function handleInput() {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 120) + 'px'
    }
  }

  function removeAttachedFile() {
    setAttachedFile(null)
    setAttachedFileText(null)
  }

  return (
    <div id="input-bar">
      <button id="attach-btn" onClick={() => fileInputRef.current?.click()}>+</button>
      <input type="file" id="file-input" ref={fileInputRef} onChange={handleFile} />
      <img id="image-preview" ref={imagePreviewRef} />
      {attachedFile && (
        <span id="file-badge" style={{ display: 'inline-flex' }}>
          <span>{attachedFile.length > 25 ? attachedFile.slice(0, 22) + '...' : attachedFile}</span>
          <span style={{ cursor: 'pointer', color: '#f87171', fontWeight: 'bold', marginLeft: 4 }} onClick={removeAttachedFile}>&#215;</span>
        </span>
      )}
      <textarea
        id="msg-input"
        ref={textareaRef}
        placeholder="Type a message..."
        rows={1}
        value={text}
        onChange={e => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        onInput={handleInput}
      />
      <button id="mic-btn" className={micRecording ? 'recording' : ''} onClick={onMicToggle}>&#127908;</button>
      <button id="send-btn" onClick={handleSend}><span className="send-icon">&#10148;</span><span className="send-text">{hasPending ? 'Queue' : 'Send'}</span></button>
    </div>
  )
}
