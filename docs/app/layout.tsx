import './global.css';
import 'nextra-theme-docs/style.css';
import { Footer, Layout, Navbar } from 'nextra-theme-docs';
import { Head } from 'nextra/components';
import { getPageMap } from 'nextra/page-map';
import { Work_Sans } from 'next/font/google';
import type { ReactNode } from 'react';

const workSans = Work_Sans({
  subsets: ['latin'],
});

export const metadata = {
  title: 'Trading Algos Documentation',
  description: 'Documentation for Trading Algos strategies and tools',
};

const logo = (
  <span className="font-bold bg-gradient-to-r from-blue-600 to-cyan-500 bg-clip-text text-transparent">
    Trading Algos
  </span>
);

export default async function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <html lang="en" dir="ltr" className={workSans.className} suppressHydrationWarning>
      <Head />
      <body className="flex flex-col min-h-screen">
        <Layout
          navbar={<Navbar logo={logo} projectLink="https://github.com/nishimweprince/trading-algos" />}
          footer={<Footer>Trading Algos Documentation</Footer>}
          docsRepositoryBase="https://github.com/nishimweprince/trading-algos/tree/main/docs"
          pageMap={await getPageMap()}
        >
          {children}
        </Layout>
      </body>
    </html>
  );
}
