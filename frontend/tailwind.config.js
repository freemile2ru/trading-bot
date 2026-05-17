/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        oracle: {
          bg: '#0a0e1a',
          card: '#111827',
          border: '#1f2937',
          accent: '#3b82f6',
          green: '#10b981',
          red: '#ef4444',
          yellow: '#f59e0b',
          purple: '#8b5cf6',
        },
      },
    },
  },
  plugins: [],
}
