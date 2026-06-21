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
        mono: ['ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      colors: {
        bg: {
          primary:   '#fafaf8',
          secondary: '#f4f4f0',
          card:      '#ffffff',
          hover:     '#f0f0ec',
          border:    '#e4e4dc',
        },
        accent: {
          DEFAULT: '#1a1a1a',
          dark:    '#000000',
        },
        success: '#16a34a',
        warning: '#b45309',
        danger:  '#dc2626',
        info:    '#1d4ed8',
        purple:  '#7c3aed',
        text: {
          primary:   '#111110',
          secondary: '#44443c',
          muted:     '#8a8a7a',
        },
      },
      animation: {
        'fade-in':  'fadeIn 0.25s ease forwards',
        'slide-up': 'slideUp 0.3s ease forwards',
        'shimmer':  'shimmer 1.6s infinite',
      },
      keyframes: {
        fadeIn:  { from: { opacity: '0' }, to: { opacity: '1' } },
        slideUp: { from: { opacity: '0', transform: 'translateY(10px)' }, to: { opacity: '1', transform: 'translateY(0)' } },
        shimmer: { '0%': { backgroundPosition: '-200% 0' }, '100%': { backgroundPosition: '200% 0' } },
      },
    },
  },
  plugins: [],
}
