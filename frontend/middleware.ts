import { NextResponse, type NextRequest } from 'next/server'

const API_BASE = (process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000').replace(/\/api$/, '')

/**
 * Route permission configuration.
 * Routes not listed here are accessible to any authenticated user.
 * The middleware checks prefixes — `/admin` matches `/admin/users`, `/admin/jobs`, etc.
 */
const ADMIN_ONLY_PREFIXES = ['/admin']
const BD_USER_ONLY_PREFIXES = ['/dashboard']

/** Routes that should not be accessible to any role (dead/orphaned routes) */
const BLOCKED_ROUTES = ['/matches', '/automation']

/**
 * Short-lived in-process cache for /auth/me lookups. The middleware runs on
 * every document/RSC request; without this, each page transition pays 2-3
 * backend round-trips (~1s each) just to re-learn the user's role. The API
 * still validates the token on every data request — this only affects page
 * routing, so 60s of role staleness is acceptable (backend caches tokens
 * for 120s anyway).
 */
const ME_CACHE = new Map<string, { user: { role?: string } | null; exp: number }>()
const ME_CACHE_TTL_MS = 60_000
const ME_CACHE_MAX = 500

export async function middleware(request: NextRequest) {
  const token = request.cookies.get('auth_token')?.value
  const url = request.nextUrl.clone()
  const { pathname } = url

  // ── 1. Static / API passthrough ────────────────────────────────────────
  if (pathname.startsWith('/_next') || pathname.startsWith('/api/')) {
    if (token && pathname.startsWith('/api/')) {
      const requestHeaders = new Headers(request.headers)
      requestHeaders.set('Authorization', `Bearer ${token}`)
      return NextResponse.next({ request: { headers: requestHeaders } })
    }
    return NextResponse.next()
  }

  // ── 2. Unauthenticated user ────────────────────────────────────────────
  if (!token) {
    if (pathname !== '/login') {
      url.pathname = '/login'
      return NextResponse.redirect(url)
    }
    return NextResponse.next()
  }

  // ── 3. Prepare auth header for downstream ──────────────────────────────
  const requestHeaders = new Headers(request.headers)
  requestHeaders.set('Authorization', `Bearer ${token}`)
  const nextResponseWithHeaders = NextResponse.next({
    request: { headers: requestHeaders },
  })

  // ── 4. Validate token with backend (page routes only) ──────────────────
  let user: { role?: string } | null = null
  let isUnauthorized = false
  let isTransientError = false

  const cached = ME_CACHE.get(token)
  if (cached && cached.exp > Date.now()) {
    user = cached.user
  } else {
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
        if (ME_CACHE.size >= ME_CACHE_MAX) ME_CACHE.clear()
        ME_CACHE.set(token, { user, exp: Date.now() + ME_CACHE_TTL_MS })
      } else if (meRes.status === 401 || meRes.status === 403) {
        isUnauthorized = true
        ME_CACHE.delete(token)
      } else {
        isTransientError = true
      }
    } catch {
      isTransientError = true
    }
  }

  // ── 5. Handle invalid / expired tokens ─────────────────────────────────
  if (!user) {
    if (isUnauthorized) {
      if (pathname === '/login') {
        const response = NextResponse.next()
        response.cookies.delete('auth_token')
        return response
      }
      const response = NextResponse.redirect(new URL('/login', request.url))
      response.cookies.delete('auth_token')
      return response
    }

    // Transient error: pass through — page-level queries show their own error states
    if (isTransientError) {
      return nextResponseWithHeaders
    }

    // Unknown state: clear and redirect
    const response = NextResponse.redirect(new URL('/login', request.url))
    response.cookies.delete('auth_token')
    return response
  }

  // ── 6. Authenticated user — enforce role-based routing ─────────────────
  const role = user.role

  // 6a. Redirect authenticated users away from login and root
  if (pathname === '/' || pathname === '/login') {
    url.pathname = role === 'admin' ? '/admin' : '/dashboard'
    return NextResponse.redirect(url)
  }

  // 6b. Block orphaned / dead routes
  if (BLOCKED_ROUTES.some(route => pathname === route || pathname.startsWith(route + '/'))) {
    url.pathname = role === 'admin' ? '/admin' : '/dashboard'
    return NextResponse.redirect(url)
  }

  // 6c. Admin-only routes: block BD users
  if (ADMIN_ONLY_PREFIXES.some(prefix => pathname.startsWith(prefix)) && role !== 'admin') {
    url.pathname = '/dashboard'
    return NextResponse.redirect(url)
  }

  // 6d. BD-user-only routes: block admins
  if (BD_USER_ONLY_PREFIXES.some(prefix => pathname.startsWith(prefix)) && role === 'admin') {
    url.pathname = '/admin'
    return NextResponse.redirect(url)
  }

  // 6e. Shared routes (/candidates, /applications) — accessible to both roles
  return nextResponseWithHeaders
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)'],
}
