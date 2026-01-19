import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';

export const baseOptions: BaseLayoutProps = {
  nav: {
    title: (
      <span className="font-bold bg-gradient-to-r from-blue-600 to-cyan-500 bg-clip-text text-transparent">
        Trading Algos
      </span>
    ),
  },
  links: [
    {
      text: 'VRVP Strategy',
      url: '/vrvp-strategy',
      active: 'nested-url',
    },
    {
      text: 'Jesse Strategies',
      url: '/jesse-strategies',
      active: 'nested-url',
    },
    {
      text: 'Tinga Tinga',
      url: '/tinga-tinga',
      active: 'nested-url',
    },
    {
      text: 'Binance Crypto',
      url: '/binance-crypto',
      active: 'nested-url',
    },
  ],
  githubUrl: 'https://github.com/nishimweprince/trading-algos',
};
