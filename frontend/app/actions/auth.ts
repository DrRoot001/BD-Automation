'use server'

import { cookies } from 'next/headers'

const API_BASE = process.env.API_URL || 'http://localhost:8000'

export async function loginAction(email: string, password: string) {
  try {
    let token = "";
    if (email === "admin@bdautomator.com" && password === "Admin123!") {
      token = "mock_admin_token";
    } else if (email === "user@bdautomator.com" && password === "User123!") {
      token = "mock_user_token";
    } else {
      return { error: 'Invalid credentials' }
    }

    // Store in httpOnly cookie
    cookies().set({
      name: 'auth_token',
      value: token,
      httpOnly: true,
      secure: true,
      sameSite: 'lax',
      path: '/',
      maxAge: 24 * 60 * 60 // matches ACCESS_TOKEN_EXPIRE_MINUTES=1440 (1 day) from backend
    })

    return { success: true }
  } catch (err) {
    return { error: 'Network error occurred during login' }
  }
}

export async function logoutAction() {
  cookies().delete('auth_token')
  return { success: true }
}

export async function getCurrentUserAction() {
  const token = cookies().get('auth_token')?.value;
  if (token === 'mock_admin_token') {
    return { id: 'admin', email: 'admin@bdautomator.com', name: 'Admin', role: 'admin' };
  } else if (token === 'mock_user_token') {
    return { id: 'user', email: 'user@bdautomator.com', name: 'User', role: 'bd_user' };
  }
  return null;
}
