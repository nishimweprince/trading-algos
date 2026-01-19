import { defineConfig, defineDocs } from 'fumadocs-mdx/config';

export const vrvpStrategy = defineDocs({
  dir: 'content/vrvp-strategy',
  docs: {
    async: true,
  },
});

export const jesseStrategies = defineDocs({
  dir: 'content/jesse-strategies',
  docs: {
    async: true,
  },
});

export const tingaTinga = defineDocs({
  dir: 'content/tinga-tinga',
  docs: {
    async: true,
  },
});

export const binanceCrypto = defineDocs({
  dir: 'content/binance-crypto',
  docs: {
    async: true,
  },
});

export default defineConfig({
  mdxOptions: {
    rehypeCodeOptions: {
      themes: {
        light: 'catppuccin-latte',
        dark: 'catppuccin-mocha',
      },
    },
  },
});
