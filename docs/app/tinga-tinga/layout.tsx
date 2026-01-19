import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import type { ReactNode } from 'react';
import { baseOptions } from '@/app/layout.config';
import { tingaTinga } from '@/lib/source';

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={tingaTinga.pageTree}
      {...baseOptions}
      sidebar={{
        banner: (
          <div className="flex items-center gap-2 rounded-lg border bg-card p-3 text-sm">
            <span className="inline-block px-2 py-0.5 text-xs font-medium rounded bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-100">
              Node.js
            </span>
            <span className="font-medium">Tinga Tinga</span>
          </div>
        ),
      }}
    >
      {children}
    </DocsLayout>
  );
}
