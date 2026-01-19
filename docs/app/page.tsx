import Link from 'next/link';

export default function HomePage() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center text-center px-4 py-16">
      <div className="max-w-4xl">
        <h1 className="text-5xl font-bold mb-6 bg-gradient-to-r from-blue-600 to-cyan-500 bg-clip-text text-transparent">
          Trading Algos Documentation
        </h1>
        <p className="text-xl text-muted-foreground mb-12 max-w-2xl mx-auto">
          Comprehensive documentation for algorithmic trading strategies including
          VRVP, Jesse Framework strategies, and more.
        </p>

        <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-2 max-w-3xl mx-auto">
          <Link
            href="/vrvp-strategy"
            className="group rounded-xl border border-border bg-card p-6 text-left transition-all hover:border-primary hover:shadow-lg"
          >
            <div className="mb-3">
              <span className="inline-block px-3 py-1 text-xs font-medium rounded-full bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-100">
                Production
              </span>
            </div>
            <h2 className="text-2xl font-semibold mb-2 group-hover:text-primary">
              VRVP Strategy
            </h2>
            <p className="text-muted-foreground">
              Multi-timeframe Forex trading system combining Supertrend, StochRSI,
              Fair Value Gap, and Volume Profile indicators.
            </p>
          </Link>

          <Link
            href="/jesse-strategies"
            className="group rounded-xl border border-border bg-card p-6 text-left transition-all hover:border-primary hover:shadow-lg"
          >
            <div className="mb-3">
              <span className="inline-block px-3 py-1 text-xs font-medium rounded-full bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-100">
                Framework
              </span>
            </div>
            <h2 className="text-2xl font-semibold mb-2 group-hover:text-primary">
              Jesse Strategies
            </h2>
            <p className="text-muted-foreground">
              Auction Market Theory based strategies using the Jesse AI framework
              including trend continuation and mean reversion.
            </p>
          </Link>

          <Link
            href="/tinga-tinga"
            className="group rounded-xl border border-border bg-card p-6 text-left transition-all hover:border-primary hover:shadow-lg"
          >
            <div className="mb-3">
              <span className="inline-block px-3 py-1 text-xs font-medium rounded-full bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-100">
                Node.js
              </span>
            </div>
            <h2 className="text-2xl font-semibold mb-2 group-hover:text-primary">
              Tinga Tinga
            </h2>
            <p className="text-muted-foreground">
              RSI crossover-based Forex/Crypto strategy with Binance integration,
              risk management, and backtesting capabilities.
            </p>
          </Link>

          <Link
            href="/binance-crypto"
            className="group rounded-xl border border-border bg-card p-6 text-left transition-all hover:border-primary hover:shadow-lg"
          >
            <div className="mb-3">
              <span className="inline-block px-3 py-1 text-xs font-medium rounded-full bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-100">
                Crypto
              </span>
            </div>
            <h2 className="text-2xl font-semibold mb-2 group-hover:text-primary">
              Binance Crypto
            </h2>
            <p className="text-muted-foreground">
              TypeScript/JavaScript strategies for Binance cryptocurrency exchange
              with utility modules for indicators.
            </p>
          </Link>
        </div>
      </div>
    </main>
  );
}
