/**
 * Monochrome Shiki themes.
 *
 * The docs are black and white, and the single amber accent is reserved for
 * state (selection, focus, the active nav item). Syntax is not a state, so it
 * gets no hue at all — tokens are separated by lightness and weight instead.
 *
 * The ramp is deliberately shallow: comments recede, keywords and function
 * names come forward in bold, and everything else sits between them. That is
 * enough structure to scan a config snippet without turning the page into a
 * second color system.
 */

/** @param {{bg: string, ramp: string[], strong: string}} opts */
function buildTheme(name, type, { bg, ramp, strong }) {
  // ramp is ordered dim → bright.
  const [dim, muted, mid, bright] = ramp;

  return {
    name,
    type,
    colors: {
      'editor.background': bg,
      'editor.foreground': bright,
    },
    settings: [
      { settings: { background: bg, foreground: bright } },

      {
        scope: ['comment', 'punctuation.definition.comment', 'string.comment'],
        settings: { foreground: dim, fontStyle: 'italic' },
      },
      {
        scope: [
          'keyword',
          'keyword.control',
          'keyword.operator.expression',
          'storage',
          'storage.type',
          'storage.modifier',
          'variable.language',
          'constant.language',
        ],
        settings: { foreground: strong, fontStyle: 'bold' },
      },
      {
        scope: ['entity.name.function', 'support.function', 'meta.function-call'],
        settings: { foreground: strong },
      },
      {
        scope: ['entity.name.tag', 'entity.name.type', 'entity.name.class', 'support.class'],
        settings: { foreground: bright, fontStyle: 'bold' },
      },
      {
        scope: ['string', 'string.quoted', 'punctuation.definition.string'],
        settings: { foreground: muted },
      },
      {
        scope: ['constant.numeric', 'constant', 'constant.character.escape'],
        settings: { foreground: mid },
      },
      {
        scope: [
          'variable',
          'variable.parameter',
          'variable.other',
          'entity.other.attribute-name',
        ],
        settings: { foreground: muted },
      },
      {
        scope: ['support.type.property-name', 'meta.object-literal.key'],
        settings: { foreground: mid },
      },
      {
        scope: ['punctuation', 'meta.brace', 'keyword.operator'],
        settings: { foreground: dim },
      },
      // Markdown / diff, so fenced examples inside the docs stay legible.
      { scope: ['markup.heading'], settings: { foreground: strong, fontStyle: 'bold' } },
      { scope: ['markup.bold'], settings: { fontStyle: 'bold' } },
      { scope: ['markup.italic'], settings: { fontStyle: 'italic' } },
      { scope: ['markup.inserted'], settings: { foreground: bright } },
      { scope: ['markup.deleted'], settings: { foreground: dim, fontStyle: 'italic' } },
    ],
  };
}

export const monochromeDark = buildTheme('trading-algos-mono-dark', 'dark', {
  bg: '#0a0a0a',
  ramp: ['#6b6b6b', '#a1a1a1', '#c9c9c9', '#ededed'],
  strong: '#ffffff',
});

export const monochromeLight = buildTheme('trading-algos-mono-light', 'light', {
  bg: '#fafafa',
  ramp: ['#8f8f8f', '#666666', '#3d3d3d', '#171717'],
  strong: '#000000',
});
