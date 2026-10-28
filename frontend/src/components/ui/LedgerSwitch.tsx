import clsx from 'clsx'
import { useLedger, type Ledger } from '../../hooks/useLedger'

const OPTIONS: [Ledger, string, string][] = [
  ['both', 'Both', 'Paper wallets and live ledgers together — judges the decisions on the most trades'],
  ['paper', 'Paper', 'Simulated fills only'],
  ['live', 'Live', 'Only trades on the live ledgers (real fills, fees and funding on the Binance account)'],
]

/** Paper / Live / Both — shared by every page that shows it. */
export function LedgerSwitch({ className }: { className?: string }) {
  const [ledger, setLedger] = useLedger()
  return (
    <div className={clsx('inline-flex rounded-lg bg-gray-800/70 p-0.5', className)} role="radiogroup" aria-label="Ledger">
      {OPTIONS.map(([k, label, title]) => (
        <button key={k} role="radio" aria-checked={ledger === k} title={title} onClick={() => setLedger(k)}
          className={clsx('px-2.5 py-1 rounded-md text-xs font-semibold',
            ledger === k ? (k === 'live' ? 'bg-rose-700 text-white' : 'bg-gray-700 text-white') : 'text-gray-400 hover:text-gray-200')}>
          {label}
        </button>
      ))}
    </div>
  )
}
