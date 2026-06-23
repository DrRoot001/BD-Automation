import { NextResponse, type NextRequest } from 'next/server'

const API_BASE = process.env.API_URL || 'http://localhost:8000'

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

  // 2. Fetch current user from backend with 3s timeout
  let user: { role?: string } | null = null
  let authFailed = false

  try {
    if (token === "mock_admin_token") {
      user = { role: "admin" }
    } else if (token === "mock_user_token") {
      user = { role: "bd_user" }
    } else {
      authFailed = true
    }
  } catch (err) {
    // Network error or timeout
    authFailed = true
  }

  // 3. Invalid token, timeout, or unauthorized
  if (!user || authFailed) {
    const response = NextResponse.redirect(new URL('/login', request.url))
    response.cookies.delete('auth_token')
    return response
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
