import type { MDXComponents } from 'mdx/types';
import { useMDXComponents as getDocsMDXComponents } from 'nextra-theme-docs';
import { Callout, Cards, Steps, Tabs } from 'nextra/components';

export function useMDXComponents(components: MDXComponents): MDXComponents {
  return {
    ...getDocsMDXComponents(),
    Callout,
    Cards,
    Steps,
    Tabs,
    ...components,
  };
}
