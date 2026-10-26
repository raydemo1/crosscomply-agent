import React from 'react'
import ReactDOM from 'react-dom/client'
import { PUBLIC_DEMO } from './runtime'
import ErrorBoundary from './components/ErrorBoundary'
import { BrowserRouter } from 'react-router-dom'
import './styles/tokens.css'
import './styles/base.css'
import './styles/primitives.css'
import './styles/layout.css'

const App = React.lazy(() => PUBLIC_DEMO ? import('./demo/DemoApp') : import('./App'))

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter><ErrorBoundary><React.Suspense fallback={<div className="app-loading">正在打开 CrossComply…</div>}><App /></React.Suspense></ErrorBoundary></BrowserRouter>
  </React.StrictMode>
)
