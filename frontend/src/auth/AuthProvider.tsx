import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, isApiError, onUnauthorized } from '../utils/api'
import type { AuthConfig, Session, TierId } from '../types/auth'

interface AuthState {
  config: AuthConfig | null
  session: Session | null
  loading: boolean
  error: string | null
}

export interface AuthValue extends AuthState {
  signInWithGoogle: (credential: string) => Promise<void>
  devLogin: (email: string, name?: string) => Promise<void>
  signOut: () => Promise<void>
  refresh: () => Promise<void>
  /** Does the signed-in tenant's tier include this feature key? Ungated local instance → everything. */
  has: (feature: string) => boolean
  tier: TierId | null
  isAdmin: boolean
}

/** No auth routes answering (the backend half not deployed yet) reads as the ungated local instance. */
const UNGATED: AuthConfig = { google_client_id: null, dev_login: false, auth_required: false }

const AuthContext = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ config: null, session: null, loading: true, error: null })
  const sessionRef = useRef<Session | null>(null)
  sessionRef.current = state.session
  const refreshing = useRef(false)

  const loadSession = useCallback(async (): Promise<Session | null> => {
    try {
      return await api.auth.me()
    } catch (e) {
      // 401 = no session where one is required; anything else is reported but still means "no session".
      if (isApiError(e) && e.status === 401) return null
      throw e
    }
  }, [])

  useEffect(() => {
    let alive = true
    ;(async () => {
      let config: AuthConfig = UNGATED
      let error: string | null = null
      try {
        config = await api.auth.config()
      } catch {
        config = UNGATED
      }
      let session: Session | null = null
      try {
        session = await loadSession()
      } catch (e) {
        if (config.auth_required) error = e instanceof Error ? e.message : 'Could not check the session'
      }
      if (alive) setState({ config, session, loading: false, error })
    })()
    return () => { alive = false }
  }, [loadSession])

  const refresh = useCallback(async () => {
    if (refreshing.current) return
    refreshing.current = true
    try {
      const session = await loadSession()
      setState(s => ({ ...s, session, error: null }))
    } catch (e) {
      setState(s => ({ ...s, session: null, error: e instanceof Error ? e.message : 'Could not check the session' }))
    } finally {
      refreshing.current = false
    }
  }, [loadSession])

  // A 401 on any other request after sign-in means the session expired: re-check, which drops to the sign-in page.
  useEffect(() => {
    onUnauthorized(() => { if (sessionRef.current) refresh() })
    return () => onUnauthorized(null)
  }, [refresh])

  const signInWithGoogle = useCallback(async (credential: string) => {
    const session = await api.auth.google(credential)
    setState(s => ({ ...s, session, error: null }))
  }, [])

  const devLogin = useCallback(async (email: string, name?: string) => {
    const session = await api.auth.devLogin(email, name)
    setState(s => ({ ...s, session, error: null }))
  }, [])

  const signOut = useCallback(async () => {
    try {
      await api.auth.logout()
    } finally {
      // Hash routes stay, so the next sign-in lands on the same tab; a stale locked tab is handled by the shell.
      setState(s => ({ ...s, session: null }))
    }
  }, [])

  const value = useMemo<AuthValue>(() => {
    const { config, session } = state
    const isAdmin = session?.user.role === 'admin'
    return {
      ...state,
      signInWithGoogle, devLogin, signOut, refresh,
      has: (feature: string) => {
        if (session) return isAdmin || session.features.includes(feature)
        return !config?.auth_required
      },
      tier: session?.tenant.tier ?? null,
      isAdmin,
    }
  }, [state, signInWithGoogle, devLogin, signOut, refresh])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthValue {
  const v = useContext(AuthContext)
  if (!v) throw new Error('useAuth must be used inside <AuthProvider>')
  return v
}
