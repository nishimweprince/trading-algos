import 'nextra-theme-docs/style.css';
// Must come last: global.css is unlayered and overrides the theme's
// @layer theme/base/components/utilities rules.
import './global.css';

import { Footer, Layout, Navbar } from 'nextra-theme-docs';
import { Head } from 'nextra/components';
import { getPageMap } from 'nextra/page-map';
import { GeistSans } from 'geist/font/sans';
import { GeistMono } from 'geist/font/mono';
import type { ReactNode } from 'react';
import { SocialLinks } from './_components/social-links';

export const metadata = {
  title: {
    default: 'Trading Algos Documentation',
    template: '%s — Trading Algos',
  },
  description:
    'Documentation for the Trading Algos strategies, execution bots, and data tooling.',
};

const logo = (
  <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
    <span style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>Trading Algos</span>
    <span
      style={{
        fontSize: '0.6875rem',
        fontWeight: 500,
        letterSpacing: '0.04em',
        textTransform: 'uppercase',
        color: 'var(--ta-text-dim)',
      }}
    >
      Docs
    </span>
  </span>
);

export default async function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      dir="ltr"
      className={`${GeistSans.variable} ${GeistMono.variable}`}
      suppressHydrationWarning
    >
      <Head
        // Drives --nextra-primary-* → --x-color-primary-*. Saturation 0 keeps
        // the theme's primary ramp pure gray — black and white only.
        color={{
          hue: { dark: 0, light: 0 },
          saturation: 0,
          lightness: { dark: 93, light: 9 },
        }}
        backgroundColor={{ dark: '#000000', light: '#ffffff' }}
        faviconGlyph="▲"
      />
      <body>
        <Layout
          navbar={
            <Navbar
              logo={logo}
              projectLink="https://github.com/nishimweprince/trading-algos"
            >
              <SocialLinks />
            </Navbar>
          }
          footer={
            <Footer>
              <div className="ta-footer">
                <span>Trading Algos Documentation</span>
              </div>
            </Footer>
          }
          docsRepositoryBase="https://github.com/nishimweprince/trading-algos/tree/main/docs"
          pageMap={await getPageMap()}
          sidebar={{ defaultMenuCollapseLevel: 1, autoCollapse: true, toggleButton: true }}
          toc={{ title: 'On this page', backToTop: 'Scroll to top', float: true }}
          editLink="Edit this page on GitHub"
          feedback={{ content: 'Question? Give us feedback', labels: 'documentation' }}
          navigation={{ next: true, prev: true }}
        >
          {children}
        </Layout>
      </body>
    </html>
  );
}
