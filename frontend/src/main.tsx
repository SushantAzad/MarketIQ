import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

function App() {
  return (
    <main>
      <p className="eyebrow">MARKETIQ / DEVELOPMENT</p>
      <h1>Financial intelligence</h1>
      <p>Repository foundation is ready for development.</p>
      <p className="notice">
        Market data, SEC ingestion, research, and risk analytics are not connected yet.
        This page verifies the frontend build only.
      </p>
    </main>
  );
}

const root = document.getElementById('root');
if (!root) throw new Error('Application root is missing');
createRoot(root).render(<StrictMode><App /></StrictMode>);
