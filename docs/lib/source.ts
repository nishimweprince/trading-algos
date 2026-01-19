import { loader } from 'fumadocs-core/source';
import { createMDXSource } from 'fumadocs-mdx';
import {
  vrvpStrategy as vrvpStrategyDocs,
  jesseStrategies as jesseStrategiesDocs,
  tingaTinga as tingaTingaDocs,
  binanceCrypto as binanceCryptoDocs,
} from '@/.source';

export const vrvpStrategy = loader({
  baseUrl: '/vrvp-strategy',
  source: createMDXSource(vrvpStrategyDocs, []),
});

export const jesseStrategies = loader({
  baseUrl: '/jesse-strategies',
  source: createMDXSource(jesseStrategiesDocs, []),
});

export const tingaTinga = loader({
  baseUrl: '/tinga-tinga',
  source: createMDXSource(tingaTingaDocs, []),
});

export const binanceCrypto = loader({
  baseUrl: '/binance-crypto',
  source: createMDXSource(binanceCryptoDocs, []),
});
