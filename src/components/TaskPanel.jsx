import { useState, useEffect } from 'react'
import * as api from '../api'

export default function TaskPanel({ onClose }) {
  const [tasks, setTasks] = useState([])
  const [newTitle, setNewTitle] = useState('')
  const [newPriority, setNewPriority] = useState('medium')
  const [showForm, setShowForm] = useState(false)

  useEffect(() => { loadTasks() }, [])

  async function loadTasks() {
    const data = await api.fetchTasks()
    setTasks(data.tasks || [])
  }

  async function handleCreate(e) {
    e.preventDefault()
    if (!newTitle.trim()) return
    await api.createTask({ title: newTitle.trim(), priority: newPriority })
    setNewTitle('')
    setNewPriority('medium')
    setShowForm(false)
    loadTasks()
  }

  async function handleToggle(task) {
    if (task.status === 'completed') {
      await api.updateTask(task.id, { status: 'pending' })
    } else {
      await api.updateTask(task.id, { status: 'completed' })
    }
    loadTasks()
  }

  async function handleDelete(tid) {
    await api.deleteTask(tid)
    loadTasks()
  }

  const priorityColors = { high: '#f87171', medium: '#fbbf24', low: '#4ade80' }

  return (
    <div id="task-panel">
      <div id="task-panel-header">
        <span>Tasks</span>
        <button onClick={() => setShowForm(!showForm)}>{showForm ? 'x' : '+'}</button>
        <button onClick={onClose} id="task-panel-close">&#10005;</button>
      </div>
      {showForm && (
        <form id="task-form" onSubmit={handleCreate}>
          <input value={newTitle} onChange={e => setNewTitle(e.target.value)} placeholder="New task..." />
          <select value={newPriority} onChange={e => setNewPriority(e.target.value)}>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
          <button type="submit">Add</button>
        </form>
      )}
      <div id="task-list">
        {tasks.map(t => (
          <div key={t.id} className={`task-item ${t.status === 'completed' ? 'done' : ''}`}>
            <input type="checkbox" checked={t.status === 'completed'} onChange={() => handleToggle(t)} />
            <span className="task-title" style={{ color: priorityColors[t.priority] || '#94a3b8' }}>{t.title}</span>
            <span className="task-status">{t.status}</span>
            {t.due_date && <span className="task-due">{new Date(t.due_date).toLocaleDateString()}</span>}
            <button className="task-delete" onClick={() => handleDelete(t.id)}>&#128465;</button>
          </div>
        ))}
      </div>
    </div>
  )
}