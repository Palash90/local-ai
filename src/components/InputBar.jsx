import { useState, useRef, useCallback } from 'react'
import { extractFile, uploadImage } from '../api'

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
const MAX_PDF_SIZE = 100 * 1024 * 1024

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result.split(',')[1])
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

export default function InputBar({ onSend, hasPending }) {
  const [text, setText] = useState('')
  const [research, setResearch] = useState(false)
  const [cpu, setCpu] = useState(false)
  const [attachedImage, setAttachedImage] = useState(null)
  const [attachedFile, setAttachedFile] = useState(null)
  const [attachedFileUrl, setAttachedFileUrl] = useState(null)
  const fileInputRef = useRef(null)
  const textareaRef = useRef(null)
  const imagePreviewRef = useRef(null)
  const sendingRef = useRef(false)

  function clearAttachments() {
    setAttachedImage(null)
    setAttachedFile(null)
    setAttachedFileUrl(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
    if (imagePreviewRef.current) imagePreviewRef.current.style.display = 'none'
  }

  const uploadFileToServer = useCallback(async (file) => {
    const b64 = await readFileAsBase64(file)
    const data = await extractFile(file.name, b64)
    if (!data.url) throw new Error(data.error || 'Could not process file')
    setAttachedFile(data.name)
    setAttachedFileUrl(data.url)
  }, [])

  const handleFile = useCallback(async (e) => {
    const file = e.target.files[0]
    if (!file) return
    const ext = '.' + file.name.split('.').pop().toLowerCase()
    const maxSize = ext === '.pdf' ? MAX_PDF_SIZE : MAX_FILE_SIZE
    if (file.size > maxSize) {
      alert('File too large (max ' + (ext === '.pdf' ? '100MB' : '10MB') + '): ' + file.name)
      e.target.value = ''
      return
    }
    clearAttachments()

    if (IMAGE_EXTS.has(ext)) {
      const reader = new FileReader()
      reader.onload = (ev) => {
        const img = new Image()
        img.onload = async () => {
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
          try {
            const res = await uploadImage(b64, 'jpg')
            setAttachedImage(res.url || b64)
          } catch {
            setAttachedImage(b64)
          }
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
      try {
        await uploadFileToServer(file)
      } catch (err) {
        clearAttachments()
        alert('Error processing file: ' + err.message)
      }
      return
    }

    if (DOC_EXTS.has(ext)) {
      try {
        await uploadFileToServer(file)
      } catch (err) {
        clearAttachments()
        alert('Error processing file: ' + err.message)
      }
      return
    }

    const isCode = confirm('Unknown file type: ' + file.name + '. Is this a code/text file?')
    if (isCode) {
      try {
        await uploadFileToServer(file)
      } catch (err) {
        clearAttachments()
        alert('Error processing file: ' + err.message)
      }
    } else {
      clearAttachments()
    }
  }, [uploadFileToServer])

  async function handleSend() {
    if (sendingRef.current) return
    const msg = text.trim()
    if (!msg && !attachedImage && !attachedFileUrl) return
    sendingRef.current = true
    let finalText = msg
    if (attachedFileUrl) {
      finalText = '[FILE: ' + attachedFileUrl + '](' + attachedFile + ')\n\n' + (msg || 'See attached file above.')
    }
    try {
      await onSend(finalText, attachedImage, research, cpu)
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
    setAttachedFileUrl(null)
  }

  function handleResearchChange(e) {
    const checked = e.target.checked
    setResearch(checked)
    if (!checked) setCpu(false)
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
      <div id="research-toggles">
        <label id="research-toggle" title="Research mode — lets the agent search and read pages for up to 50 tool rounds until your question is fully answered.">
          <input type="checkbox" checked={research} onChange={handleResearchChange} />
          Research
        </label>
        <label id="cpu-toggle" className={research ? '' : 'disabled'} title={research ? "Run the research on the CPU-backed server instead of the GPU." : "Only available with Research mode."}>
          <input type="checkbox" checked={cpu} disabled={!research} onChange={e => setCpu(e.target.checked)} />
          CPU
        </label>
      </div>
      <button id="send-btn" onClick={handleSend}><span className="send-icon">&#10148;</span><span className="send-text">{hasPending ? 'Queue' : 'Send'}</span></button>
    </div>
  )
}
