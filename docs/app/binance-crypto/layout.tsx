import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import type { ReactNode } from 'react';
import { baseOptions } from '@/app/layout.config';
import { binanceCrypto } from '@/lib/source';

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={binanceCrypto.pageTree}
      {...baseOptions}
      sidebar={{
        banner: (
          <div className="flex items-center gap-2 rounded-lg border bg-card p-3 text-sm">
            <span className="inline-block px-2 py-0.5 text-xs font-medium rounded bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-100">
              Crypto
            </span>
            <span className="font-medium">Binance Crypto</span>
          </div>
        ),
      }}
    >
      {children}
    </DocsLayout>
  );
}
