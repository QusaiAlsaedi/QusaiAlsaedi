/**
 * Generate simple SVG-based PNG placeholder icons for the PWA.
 * Run: node generate-icons.js
 *
 * In production, replace these with proper app icons.
 * For now we create simple SVG files that browsers accept.
 */
const fs = require('fs');
const path = require('path');

function makeSVG(size) {
  const r = size / 2;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
  <rect width="${size}" height="${size}" rx="${size * 0.15}" fill="#1e40af"/>
  <g transform="translate(${size/2}, ${size/2}) scale(${size/48})">
    <path d="M0 2a3 3 0 0 0 3-3V-7a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0h-2a7 7 0 0 0 6 6.93V9h2V5.93A7 7 0 0 0 7-1H5z" fill="white"/>
  </g>
</svg>`;
}

// Write as SVG (browsers handle SVG icons in manifest)
fs.writeFileSync(path.join(__dirname, 'public', 'icon-192.svg'), makeSVG(192));
fs.writeFileSync(path.join(__dirname, 'public', 'icon-512.svg'), makeSVG(512));
console.log('Icons generated (SVG).');
