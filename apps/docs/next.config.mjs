import nextra from 'nextra';
import { monochromeDark, monochromeLight } from './lib/shiki-monochrome.mjs';

const withNextra = nextra({
  defaultShowCopyCode: true,
  search: { codeblocks: true },
  mdxOptions: {
    // Dual themes emit --shiki-light / --shiki-dark custom properties, so the
    // code blocks follow the theme switch without a re-render.
    rehypePrettyCodeOptions: {
      theme: { light: monochromeLight, dark: monochromeDark },
    },
  },
});

/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,
  async redirects() {
    return [
      {
        source: '/ctrader-markets',
        destination: '/execution-service',
        permanent: true,
      },
      {
        source: '/ctrader-markets/getting-started',
        destination: '/execution-service/getting-started',
        permanent: true,
      },
      {
        source: '/ctrader-markets/architecture',
        destination: '/execution-service/architecture',
        permanent: true,
      },
      {
        source: '/ctrader-markets/api',
        destination: '/execution-service/api',
        permanent: true,
      },
      {
        source: '/ctrader-markets/configuration',
        destination: '/execution-service/configuration',
        permanent: true,
      },
      {
        source: '/ctrader-markets/:path*',
        destination: '/execution-service',
        permanent: true,
      },
      {
        source: '/mt5-trader',
        destination: '/execution-service',
        permanent: true,
      },
      {
        source: '/mt5-trader/getting-started',
        destination: '/execution-service/getting-started',
        permanent: true,
      },
      {
        source: '/mt5-trader/architecture',
        destination: '/execution-service/architecture',
        permanent: true,
      },
      {
        source: '/mt5-trader/api',
        destination: '/execution-service/api',
        permanent: true,
      },
      {
        source: '/mt5-trader/:path*',
        destination: '/execution-service',
        permanent: true,
      },
    ];
  },
};

export default withNextra(config);
