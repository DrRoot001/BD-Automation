/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './hooks/**/*.{js,ts,jsx,tsx,mdx}',
    './lib/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
      colors: {
        bg: {
          primary: '#0a0a0f',
          secondary: '#12121a',
          card: '#16161f',
          hover: '#1e1e2a',
          border: '#252535',
        },
        accent: {
          DEFAULT: '#6366f1',
          light: '#818cf8',
          dark: '#4f46e5',
          glow: 'rgba(99,102,241,0.25)',
        },
        success: { DEFAULT: '#10b981', light: '#34d399', bg: 'rgba(16,185,129,0.12)' },
        warning: { DEFAULT: '#f59e0b', light: '#fbbf24', bg: 'rgba(245,158,11,0.12)' },
        danger:  { DEFAULT: '#ef4444', light: '#f87171', bg: 'rgba(239,68,68,0.12)' },
        info:    { DEFAULT: '#3b82f6', light: '#60a5fa', bg: 'rgba(59,130,246,0.12)' },
        purple:  { DEFAULT: '#a855f7', light: '#c084fc', bg: 'rgba(168,85,247,0.12)' },
        text: {
          primary:   '#f1f5f9',
          secondary: '#94a3b8',
          muted:     '#64748b',
        },
      },
      backgroundImage: {
        'gradient-radial':  'radial-gradient(var(--tw-gradient-stops))',
        'gradient-card':    'linear-gradient(135deg, #16161f 0%, #12121a 100%)',
        'gradient-accent':  'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)',
        'gradient-success': 'linear-gradient(135deg, #10b981 0%, #059669 100%)',
      },
      boxShadow: {
        'card':        '0 4px 24px rgba(0,0,0,0.4)',
        'card-hover':  '0 8px 40px rgba(0,0,0,0.5)',
        'accent':      '0 0 30px rgba(99,102,241,0.3)',
        'glow-green':  '0 0 20px rgba(16,185,129,0.25)',
        'glow-purple': '0 0 20px rgba(168,85,247,0.25)',
      },
      animation: {
        'fade-in':       'fadeIn 0.4s ease forwards',
        'slide-up':      'slideUp 0.4s ease forwards',
        'pulse-slow':    'pulse 3s ease-in-out infinite',
        'spin-slow':     'spin 3s linear infinite',
        'number-count':  'numberCount 0.6s ease forwards',
        'shimmer':       'shimmer 2s infinite',
        'dot-pulse':     'dotPulse 1.4s ease-in-out infinite',
      },
      keyframes: {
        fadeIn: {
          from: { opacity: '0' },
          to:   { opacity: '1' },
        },
        slideUp: {
          from: { opacity: '0', transform: 'translateY(16px)' },
          to:   { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '0%':   { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        dotPulse: {
          '0%, 80%, 100%': { transform: 'scale(0.8)', opacity: '0.5' },
          '40%':           { transform: 'scale(1)',   opacity: '1'   },
        },
      },
    },
  },
  plugins: [],
}
