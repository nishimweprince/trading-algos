import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import type { ReactNode } from 'react';
import { baseOptions } from '@/app/layout.config';
import { jesseStrategies } from '@/lib/source';

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={jesseStrategies.pageTree}
      {...baseOptions}
      sidebar={{
        banner: (
          <div className="flex items-center gap-2 rounded-lg border bg-card p-3 text-sm">
            <span className="inline-block px-2 py-0.5 text-xs font-medium rounded bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-100">
              Framework
            </span>
            <span className="font-medium">Jesse Strategies</span>
          </div>
        ),
      }}
    >
      {children}
    </DocsLayout>
  );
}
