/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: ['./app/templates/**/*.html'],
  theme: {
    extend: {
      colors: {
        // Semantic design tokens — values live in tailwind.input.css as CSS
        // variables so the whole UI can flip between light/dark instantly.
        app: {
          bg: 'var(--app-bg)',
          surface: 'var(--app-surface)',
          raised: 'var(--app-raised)',
        },
        line: {
          DEFAULT: 'var(--line)',
          strong: 'var(--line-strong)',
        },
        ink: {
          DEFAULT: 'var(--fg)',
          muted: 'var(--fg-muted)',
          faint: 'var(--fg-faint)',
        },
        brand: {
          DEFAULT: 'var(--brand)',
          hover: 'var(--brand-hover)',
          soft: 'var(--brand-soft)',
          fg: 'var(--brand-fg)',
          line: 'var(--brand-line)',
        },
        ok: {
          DEFAULT: 'var(--ok)',
          soft: 'var(--ok-soft)',
          fg: 'var(--ok-fg)',
          line: 'var(--ok-line)',
        },
        danger: {
          DEFAULT: 'var(--danger)',
          soft: 'var(--danger-soft)',
          fg: 'var(--danger-fg)',
          line: 'var(--danger-line)',
        },
        warn: {
          DEFAULT: 'var(--warn)',
          soft: 'var(--warn-soft)',
          fg: 'var(--warn-fg)',
          line: 'var(--warn-line)',
        },
      },
    },
  },
};

