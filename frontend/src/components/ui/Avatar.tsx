import { cn } from "@/lib/cn";

function initialsFrom(label: string | null | undefined): string {
  if (!label) return "?";
  const source = label.includes("@") ? label.split("@", 1)[0] : label;
  const parts = source.split(/[\s._-]+/).filter(Boolean);
  const take = parts.slice(0, 2).map((p) => p[0]!.toUpperCase()).join("");
  return take || source[0]!.toUpperCase();
}

export function Avatar({
  label,
  size = "md",
  className,
}: {
  label?: string | null;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const dims = size === "sm" ? "size-7 text-xs" : size === "lg" ? "size-12 text-lg" : "size-9 text-sm";
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex items-center justify-center rounded-full bg-brand-600 font-semibold text-white",
        dims,
        className,
      )}
    >
      {initialsFrom(label)}
    </span>
  );
}
