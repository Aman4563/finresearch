import Link from "next/link";
import type { ReactNode } from "react";

import { cx } from "@/components/ui";

const V = {
  primary: "bg-brand text-brand-fg hover:bg-brand-strong shadow-sm hover:shadow-glow",
  secondary: "bg-card text-foreground ring-1 ring-inset ring-border hover:bg-card-hover hover:ring-border-strong",
  ghost: "text-muted hover:bg-background-subtle hover:text-foreground",
};

/** A link that looks like `Button` (a <button> inside an <a> is invalid HTML). */
export function LinkButton({ href, children, icon, variant = "secondary", size = "sm", className }: {
  href: string; children: ReactNode; icon?: ReactNode; variant?: keyof typeof V; size?: "sm" | "md"; className?: string;
}) {
  return (
    <Link
      href={href}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition duration-150 active:scale-[0.97]",
        size === "sm" ? "h-8 px-3 text-xs" : "h-10 px-4 text-sm",
        V[variant],
        className,
      )}
    >
      {icon}
      {children}
    </Link>
  );
}
