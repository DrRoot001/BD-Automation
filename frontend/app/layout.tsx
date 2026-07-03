import type { Metadata } from 'next'
import './globals.css'
import { ReactQueryProvider } from '@/lib/providers'
import { ClientLayout } from '@/components/shared/ClientLayout'

export const metadata: Metadata = {
  title: 'BD Automator — Dashboard',
  description:
    'AI-powered job application automation platform. Discover, match, and apply to jobs automatically with intelligent resume tailoring and browser automation.',
  keywords: ['job applications', 'automation', 'AI', 'dashboard', 'resume', 'job search'],
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,300..700;1,14..32,300..700&display=swap"
          rel="stylesheet"
        />
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
