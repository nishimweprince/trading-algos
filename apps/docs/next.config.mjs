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
};

export default withNextra(config);
