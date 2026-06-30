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

  // 2. Fetch current user from backend with 8s timeout
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

  // 3. Action based on auth validation
  if (!user) {
    const isLoginPath = url.pathname.startsWith('/login')
    if (isUnauthorized || !isTransientError) {
      if (isLoginPath) {
        const response = NextResponse.next()
        response.cookies.delete('auth_token')
        return response
      }
      const response = NextResponse.redirect(new URL('/login', request.url))
      response.cookies.delete('auth_token')
      return response
    } else {
      // Transient network timeout or server error. Redirect to login if not already there, but DO NOT delete token.
      if (isLoginPath) {
        return NextResponse.next()
      }
      return NextResponse.redirect(new URL('/login', request.url))
    }
  }

  // 4. Role-based routing
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

  // 5. Inject Authorization header for all /api requests sent from the client
  // The client hits Next.js /api/..., the middleware adds the header, and then the rewrite forwards it.
  const requestHeaders = new Headers(request.headers)
  requestHeaders.set('Authorization', `Bearer ${token}`)

  return NextResponse.next({
    request: {
      headers: requestHeaders,
    },
  })
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)'],
}
