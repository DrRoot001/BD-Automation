import { NextResponse, type NextRequest } from 'next/server'

const API_BASE = (process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000').replace(/\/api$/, '')

export async function middleware(request: NextRequest) {
  const token = request.cookies.get('auth_token')?.value
  const url = request.nextUrl.clone()

  // 1. Unauthenticated user
  if (!token) {
    if (!url.pathname.startsWith('/login') && !url.pathname.startsWith('/api/')) {
      url.pathname = '/login'
      return NextResponse.redirect(url)
    }
    return NextResponse.next()
  }

  // 2. Always inject Authorization header for all requests early so that if we return NextResponse.next()
  // it includes the token. The Next.js rewrite will forward it to the backend.
  const requestHeaders = new Headers(request.headers)
  requestHeaders.set('Authorization', `Bearer ${token}`)
  const nextResponseWithHeaders = NextResponse.next({
    request: {
      headers: requestHeaders,
    },
  })

  // If it's an API call, we DO NOT need to check /api/auth/me here because the backend handles it.
  if (url.pathname.startsWith('/api/')) {
    return nextResponseWithHeaders
  }

  // 3. Fetch current user from backend with 3s timeout (for page routes only)
  let user: { role?: string } | null = null
  let isUnauthorized = false
  let isTransientError = false

  try {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 3000)
    const meRes = await fetch(`${API_BASE}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
      cache: 'no-store',
    })
    clearTimeout(timeout)
    if (meRes.ok) {
      user = await meRes.json()
    } else {
      if (meRes.status === 401 || meRes.status === 403) {
        isUnauthorized = true
      } else {
        isTransientError = true
      }
    }
  } catch {
    isTransientError = true
  }

  // 4. Action based on auth validation
  if (!user) {
    const isLoginPath = url.pathname.startsWith('/login')
    if (isUnauthorized) {
      if (isLoginPath) {
        const response = NextResponse.next()
        response.cookies.delete('auth_token')
        return response
      }
      const response = NextResponse.redirect(new URL('/login', request.url))
      response.cookies.delete('auth_token')
      return response
    }
    
    // Transient error (network timeout, backend restart): pass through rather than
    // forcing a login redirect — the page-level queries will show their own error states.
    if (isTransientError) {
      return nextResponseWithHeaders
    }
    
    // Unknown state (shouldn't happen): clear and redirect
    const response = NextResponse.redirect(new URL('/login', request.url))
    response.cookies.delete('auth_token')
    return response
  }

  // 5. Role-based routing
  const role = user.role

  if (url.pathname === '/' || url.pathname.startsWith('/login')) {
    if (role === 'admin') {
      url.pathname = '/admin'
    } else {
      url.pathname = '/dashboard'
    }
    return NextResponse.redirect(url)
  }

  if (url.pathname.startsWith('/admin') && role !== 'admin') {
    url.pathname = '/dashboard'
    return NextResponse.redirect(url)
  }

  return nextResponseWithHeaders
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)'],
}
