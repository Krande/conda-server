/**
 * conda-server logo mark: an isometric package cube. Single-tone via
 * `currentColor`, so callers tint it with a `text-brand-*` class and it tracks
 * the active accent palette.
 */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinejoin="round"
      className={className}
      aria-hidden
    >
      <path d="M16 3 27 9v14l-11 6L5 23V9z" fill="currentColor" fillOpacity="0.12" />
      <path d="M16 3 27 9v14l-11 6L5 23V9z" />
      <path d="M16 3v13L5 9M16 16l11-7M16 16v13" />
    </svg>
  );
}
