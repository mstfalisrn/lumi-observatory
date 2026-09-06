import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"
const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold tracking-wide transition-all duration-200 ease-[cubic-bezier(0.32,0.72,0,1)] focus:outline-none focus:ring-2 focus:ring-violet-500/20 focus:ring-offset-0",
  {
    variants: {
      variant: {
        default: "border-transparent bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-[0_2px_8px_rgba(99,102,241,0.3)] hover:shadow-[0_4px_12px_rgba(99,102,241,0.4)] hover:from-violet-700 hover:to-indigo-700",
        secondary: "border-transparent bg-secondary text-secondary-foreground backdrop-blur-sm hover:bg-secondary/80 shadow-sm",
        destructive: "border-transparent bg-gradient-to-br from-red-500 to-rose-600 text-white shadow-[0_2px_8px_rgba(239,68,68,0.3)] hover:from-red-600 hover:to-rose-700",
        outline: "text-foreground bg-background/50 backdrop-blur-sm border-border hover:bg-accent hover:border-violet-200 dark:hover:border-white/10",
        success: "border-transparent bg-gradient-to-br from-emerald-500 to-teal-600 text-white shadow-[0_2px_8px_rgba(16,185,129,0.3)]",
        warning: "border-transparent bg-gradient-to-br from-amber-400 to-orange-500 text-white shadow-[0_2px_8px_rgba(245,158,11,0.3)]",
        violet: "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-800/50 dark:bg-violet-950/40 dark:text-violet-300 shadow-sm",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
