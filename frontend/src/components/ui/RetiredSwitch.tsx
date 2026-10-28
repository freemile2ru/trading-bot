import clsx from 'clsx'

/** "Include retired strategies" toggle, shown on the Trades and Portfolio pages. */
export function RetiredSwitch({ on, onChange, hidden }: { on: boolean; onChange: (v: boolean) => void; hidden?: number }) {
  return (
    <label className="inline-flex items-center gap-2 text-xs text-gray-400 cursor-pointer select-none"
           title="The retired order-flow scalper's trades stay on record; they're left out of these views unless you include them">
      <button role="switch" aria-checked={on} onClick={() => onChange(!on)}
        className={clsx('relative inline-flex h-4 w-7 shrink-0 rounded-full transition-colors', on ? 'bg-green-500' : 'bg-gray-700')}>
        <span className={clsx('absolute top-0.5 h-3 w-3 rounded-full bg-white transition-transform', on ? 'translate-x-3.5' : 'translate-x-0.5')} />
      </button>
      <span>
        Include retired strategies
        {!on && hidden ? <span className="text-gray-600"> · {hidden.toLocaleString()} trades hidden</span> : null}
      </span>
    </label>
  )
}
