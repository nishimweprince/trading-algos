# Trading Algos Documentation

Documentation site for Trading Algos built with [Fuma Docs](https://fumadocs.vercel.app/), Next.js, and MDX.

## Getting Started

### Prerequisites

- Node.js 18+
- npm, yarn, or pnpm

### Installation

```bash
# Install dependencies
npm install

# Start development server
npm run dev

# Build for production
npm run build

# Start production server
npm start
```

The documentation will be available at `http://localhost:3000`.

## Project Structure

```
docs/
├── app/                    # Next.js App Router
│   ├── layout.tsx          # Root layout
│   ├── page.tsx            # Home page
│   ├── vrvp-strategy/      # VRVP Strategy section
│   ├── jesse-strategies/   # Jesse Strategies section
│   ├── tinga-tinga/        # Tinga Tinga section
│   └── binance-crypto/     # Binance Crypto section
├── content/                # MDX documentation files
│   ├── vrvp-strategy/      # VRVP docs
│   ├── jesse-strategies/   # Jesse docs
│   ├── tinga-tinga/        # Tinga Tinga docs
│   └── binance-crypto/     # Binance Crypto docs
├── lib/                    # Utility functions
│   └── source.ts           # Documentation sources
├── source.config.ts        # Fumadocs MDX config
├── next.config.mjs         # Next.js config
├── tailwind.config.ts      # Tailwind CSS config
└── package.json
```

## Documentation Sections

| Section | Path | Description |
|---------|------|-------------|
| VRVP Strategy | `/vrvp-strategy` | Multi-timeframe Forex trading system |
| Jesse Strategies | `/jesse-strategies` | Auction Market Theory strategies |
| Tinga Tinga | `/tinga-tinga` | RSI-based Node.js strategy |
| Binance Crypto | `/binance-crypto` | TypeScript crypto strategies |

## Adding Documentation

### Creating a New Page

1. Create an MDX file in the appropriate `content/` directory
2. Add frontmatter with title and description:

```mdx
---
title: Page Title
description: Brief description
---

# Page Title

Content here...
```

3. Update the `meta.json` file to include the new page in navigation

### MDX Features

- **Code blocks** with syntax highlighting
- **Callouts** for warnings and tips
- **Tables** for structured data
- **Links** between documentation pages

## Deployment

### Vercel (Recommended)

```bash
npm run build
vercel deploy
```

### Docker

```dockerfile
FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["npm", "start"]
```

## Contributing

1. Edit MDX files in the `content/` directory
2. Preview changes with `npm run dev`
3. Submit a pull request

## License

MIT
