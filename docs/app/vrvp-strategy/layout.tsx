import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import type { ReactNode } from 'react';
import { baseOptions } from '@/app/layout.config';
import { vrvpStrategy } from '@/lib/source';

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={vrvpStrategy.pageTree}
      {...baseOptions}
      sidebar={{
        banner: (
          <div className="flex items-center gap-2 rounded-lg border bg-card p-3 text-sm">
            <span className="inline-block px-2 py-0.5 text-xs font-medium rounded bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-100">
              Production
            </span>
            <span className="font-medium">VRVP Strategy</span>
          </div>
        ),
      }}
    >
      {children}
    </DocsLayout>
  );
}
