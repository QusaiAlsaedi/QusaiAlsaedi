import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';

// Global monochrome terminal style
const style = document.createElement('style');
style.textContent = `
  * { box-sizing: border-box; }
  body { margin: 0; background: #0a0a0a; color: #aaa; font-family: 'Courier New', monospace; }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: #111; }
  ::-webkit-scrollbar-thumb { background: #333; }
  input, select, button { outline: none; }
  input:focus, select:focus { border-color: #444 !important; }
`;
document.head.appendChild(style);

const root = ReactDOM.createRoot(document.getElementById('root') as HTMLElement);
root.render(<React.StrictMode><App /></React.StrictMode>);
