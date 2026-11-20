import { useEffect, useRef, useState } from 'react'
import { ChevronDown, CreditCard, LogOut, Settings as SettingsIcon } from 'lucide-react'
import clsx from 'clsx'
import { useAuth } from '../../auth/AuthProvider'
import { TierBadge } from './TierBadge'

/** Header account menu: avatar, name, tier pill; Plans & billing, Settings, Sign out. */
export function AccountMenu({ onOpenPricing, onOpenSettings }: { onOpenPricing: () => void; onOpenSettings: () => void }) {
  const { session, config, signOut } = useAuth()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); window.removeEventListener('keydown', onKey) }
  }, [open])

  if (!session) return null
  const { user, tenant } = session
  const initial = (user.name || user.email || '?').trim().charAt(0).toUpperCase()
  const roleNote = user.role === 'admin' ? 'admin' : user.role === 'owner' ? 'owner' : null
  const canSignOut = !!config?.auth_required || !!config?.dev_login || config?.google_client_id != null

  return (
    <div ref={ref} className="relative">
      <button onClick={() => setOpen(o => !o)} aria-haspopup="menu" aria-expanded={open}
        className="flex items-center gap-2 rounded-lg px-1.5 py-1 hover:bg-gray-800 transition-colors">
        {user.picture ? (
          <img src={user.picture} alt="" referrerPolicy="no-referrer" className="w-7 h-7 rounded-full border border-gray-700" />
        ) : (
          <span className="w-7 h-7 rounded-full bg-gray-700 text-white text-xs font-bold flex items-center justify-center">{initial}</span>
        )}
        <span className="hidden md:block text-left leading-tight">
          <span className="block text-xs text-gray-200 font-semibold max-w-[9rem] truncate">{user.name || user.email}</span>
          <span className="block text-[10px] text-gray-500 max-w-[9rem] truncate">{roleNote ?? tenant.name}</span>
        </span>
        <TierBadge tier={tenant.tier} />
        <ChevronDown className={clsx('w-3 h-3 text-gray-500 transition-transform', open && 'rotate-180')} />
      </button>

      {open && (
        <div role="menu" className="absolute right-0 mt-1.5 w-64 card p-2 shadow-xl z-30 space-y-1">
          <div className="px-2 py-1.5">
            <div className="text-xs text-gray-100 font-semibold truncate">{user.name || user.email}</div>
            <div className="text-[11px] text-gray-500 truncate">{user.email}</div>
            <div className="mt-1.5 flex items-center gap-2 text-[11px] text-gray-400">
              <TierBadge tier={tenant.tier} />
              <span className="truncate">{tenant.name}</span>
              {roleNote && <span className="ml-auto px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 text-[10px] font-semibold uppercase">{roleNote}</span>}
            </div>
            {tenant.status !== 'active' && (
              <div className={clsx('mt-1 text-[10px] font-semibold', tenant.status === 'past_due' ? 'text-amber-300' : 'text-gray-400')}>
                billing {tenant.status.replace('_', ' ')}
              </div>
            )}
          </div>
          <div className="border-t border-gray-800" />
          <MenuItem icon={CreditCard} label="Plans & billing" onClick={() => { setOpen(false); onOpenPricing() }} />
          <MenuItem icon={SettingsIcon} label="Settings" onClick={() => { setOpen(false); onOpenSettings() }} />
          {canSignOut && (
            <>
              <div className="border-t border-gray-800" />
              <MenuItem icon={LogOut} label="Sign out" onClick={() => { setOpen(false); signOut() }} />
            </>
          )}
        </div>
      )}
    </div>
  )
}

function MenuItem({ icon: Icon, label, onClick }: { icon: typeof CreditCard; label: string; onClick: () => void }) {
  return (
    <button role="menuitem" onClick={onClick}
      className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs text-gray-300 hover:bg-gray-800 hover:text-white transition-colors text-left">
      <Icon className="w-3.5 h-3.5 text-gray-500" /> {label}
    </button>
  )
}
