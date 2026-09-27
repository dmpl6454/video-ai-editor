import { createRoot } from 'react-dom/client'
import './fonts.css'
import './styles.css'
import App from './App'
import { useStore } from './store'
import { installTestHook } from './lib/testHook'

installTestHook(window.location.search, useStore, window)

createRoot(document.getElementById('root')!).render(<App />)
