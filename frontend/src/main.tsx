import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles/index.css'

// Apply shell/theme before first paint to avoid FOUC
;(() => {
  const root = document.documentElement
  const savedShell = localStorage.getItem('ui-shell')
  const shell = savedShell === 'solstice' ? 'solstice' : 'craft-paper'
  const savedTheme = localStorage.getItem('ui-theme')
  let theme = savedTheme === 'light' || savedTheme === 'dark' ? savedTheme : null
  if (!theme) {
    const legacy = localStorage.getItem('theme')
    if (legacy === 'light' || legacy === 'dark') theme = legacy
    else theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  }
  root.setAttribute('data-shell', shell)
  root.setAttribute('data-theme', theme)
})()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
