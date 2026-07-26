function statusState(msg) {
  if (!msg) return 'thinking'
  if (/^Searching/.test(msg)) return 'search'
  if (/^Generating image/.test(msg)) return 'generate-image'
  if (/^Editing image/.test(msg)) return 'edit-image'
  return 'thinking'
}

const icons = {
  search: '\uD83D\uDD0D',
  'generate-image': '\uD83C\uDFA8',
  'edit-image': '\u270F\uFE0F',
  thinking: '\uD83D\uDCAD',
}

export default function StatusBox({ message }) {
  const state = statusState(message)
  return (
    <div className="status-box open" data-state={state}>
      <div className="status-header">
        <div className="left-group">
          <span className="status-icon">{icons[state]}</span>
          <span className="status-text">{message || 'Thinking...'}</span>
          <div className="spinner"></div>
        </div>
      </div>
    </div>
  )
}
