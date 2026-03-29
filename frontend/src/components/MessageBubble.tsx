import React from 'react'

interface Props {
  role: 'user' | 'assistant' | 'tool'
  content: string
  toolName?: string
}

export default function MessageBubble({ role, content, toolName }: Props) {
  if (role === 'tool') {
    return (
      <div className="message tool">
        <span className="tool-badge">🔧 {toolName}</span>
      </div>
    )
  }
  return (
    <div className={`message ${role}`}>
      <div className="bubble">{content}</div>
    </div>
  )
}
