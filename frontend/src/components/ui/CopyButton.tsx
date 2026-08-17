import { useState } from "react";
import { Button, type ButtonProps } from "./Button";

export function CopyButton({
  value,
  children,
  ...rest
}: ButtonProps & { value: string }) {
  const [copied, setCopied] = useState(false);

  const handleClick = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Browser blocked clipboard access — silent fallback.
    }
  };

  return (
    <Button variant="ghost" size="sm" onClick={handleClick} {...rest}>
      {copied ? "Copied" : (children ?? "Copy")}
    </Button>
  );
}
