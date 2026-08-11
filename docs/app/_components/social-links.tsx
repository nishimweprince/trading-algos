/**
 * Author profile links for the site navbar.
 *
 * Edit `PROFILES` to change handles — the markup below reads from it, so the
 * URLs live in exactly one place.
 */

type Profile = {
  name: string;
  href: string;
  /** Single SVG path, 24x24 viewBox, drawn in currentColor. */
  path: string;
};

const PROFILES: Profile[] = [
  {
    name: 'GitHub',
    href: 'https://github.com/nishimweprince',
    path: 'M12 2C6.48 2 2 6.58 2 12.25c0 4.53 2.87 8.37 6.84 9.73.5.1.68-.22.68-.49l-.01-1.7c-2.78.62-3.37-1.38-3.37-1.38-.45-1.19-1.11-1.5-1.11-1.5-.91-.64.07-.63.07-.63 1 .07 1.53 1.06 1.53 1.06.89 1.56 2.34 1.11 2.91.85.09-.66.35-1.11.63-1.37-2.22-.26-4.56-1.14-4.56-5.07 0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.3.1-2.71 0 0 .84-.28 2.75 1.05a9.3 9.3 0 0 1 5 0c1.91-1.33 2.75-1.05 2.75-1.05.55 1.41.2 2.45.1 2.71.64.72 1.03 1.63 1.03 2.75 0 3.94-2.34 4.81-4.57 5.06.36.32.68.94.68 1.9l-.01 2.82c0 .27.18.6.69.49A10.06 10.06 0 0 0 22 12.25C22 6.58 17.52 2 12 2Z',
  },
  {
    name: 'LinkedIn',
    href: 'https://www.linkedin.com/in/nishimweprince',
    path: 'M6.94 5a1.94 1.94 0 1 1-3.88 0 1.94 1.94 0 0 1 3.88 0ZM3.998 8.5h3.88V21h-3.88V8.5Zm6.5 0h3.72v1.71h.05c.52-.94 1.79-1.93 3.68-1.93 3.93 0 4.66 2.5 4.66 5.76V21h-3.88v-5.87c0-1.4-.03-3.2-2-3.2-2 0-2.31 1.52-2.31 3.1V21h-3.87V8.5Z',
  },
  {
    name: 'X',
    href: 'https://x.com/nishimweprince',
    path: 'M17.53 3h3.06l-6.69 7.64L21.75 21h-6.16l-4.82-6.3L5.24 21H2.18l7.15-8.17L2.25 3h6.32l4.36 5.77L17.53 3Zm-1.07 16.16h1.69L7.62 4.74H5.8l10.66 14.42Z',
  },
  {
    name: 'WhatsApp',
    href: 'https://wa.me/18163529842',
    path: 'M12.04 2a9.9 9.9 0 0 0-8.5 14.95L2 22l5.2-1.36A9.9 9.9 0 1 0 12.04 2Zm0 1.82a8.07 8.07 0 1 1-4.1 15.02l-.3-.18-3.09.81.82-3-.19-.31A8.07 8.07 0 0 1 12.04 3.82Zm-3.2 4.05c-.16 0-.42.06-.64.3-.22.24-.84.82-.84 2s.86 2.32.98 2.48c.12.16 1.7 2.6 4.13 3.64.58.25 1.03.4 1.38.51.58.19 1.11.16 1.53.1.47-.07 1.43-.59 1.63-1.15.2-.56.2-1.04.14-1.14-.06-.1-.22-.16-.46-.28-.24-.12-1.43-.7-1.65-.78-.22-.08-.38-.12-.54.12-.16.24-.62.78-.76.94-.14.16-.28.18-.52.06-.24-.12-1.02-.37-1.94-1.19-.72-.64-1.2-1.42-1.34-1.66-.14-.24-.02-.37.1-.49.11-.11.24-.28.36-.42.12-.14.16-.24.24-.4.08-.16.04-.3-.02-.42-.06-.12-.54-1.3-.74-1.78-.19-.46-.39-.4-.54-.41h-.46Z',
  },
];

export function SocialLinks() {
  return (
    <nav aria-label="Author profiles" className="ta-social">
      {PROFILES.map(({ name, href, path }) => (
        <a
          key={name}
          href={href}
          target="_blank"
          rel="me noopener noreferrer"
          aria-label={name}
          title={name}
        >
          <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">
            <path d={path} />
          </svg>
        </a>
      ))}
    </nav>
  );
}
