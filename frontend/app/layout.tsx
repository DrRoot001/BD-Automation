import type { Metadata } from 'next'
import './globals.css'
import { ReactQueryProvider } from '@/lib/providers'
import { ClientLayout } from '@/components/ClientLayout'

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
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      </head>
      <body className="bg-bg-primary text-text-primary font-sans">
        <ReactQueryProvider>
          <ClientLayout>
            {children}
          </ClientLayout>
        </ReactQueryProvider>
      </body>
    </html>
  )
}
