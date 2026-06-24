import { createClient } from '@supabase/supabase-js'

// Supabase client for DB only — auth is handled by backend JWT via /api/auth endpoints
const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || ''
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || ''

export const supabase = createClient(supabaseUrl, supabaseAnonKey)
