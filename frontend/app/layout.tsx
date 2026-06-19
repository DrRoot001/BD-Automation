import type { Metadata } from 'next'
import './globals.css'
import { ReactQueryProvider } from '@/lib/providers'

export const metadata: Metadata = {
  title: 'BD Automator — Dashboard',
  description:
    'Real-time job application analytics dashboard powered by AI email intelligence.',
  keywords: ['job applications', 'automation', 'AI', 'dashboard'],
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" className="dark">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="bg-bg-primary text-text-primary font-sans">
        <ReactQueryProvider>{children}</ReactQueryProvider>
      </body>
    </html>
  )
}
