import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl text-sm font-medium transition-all duration-200 ease-[cubic-bezier(0.32,0.72,0,1)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/20 focus-visible:ring-offset-0 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0 active:scale-[0.97] select-none",
  {
    variants: {
      variant: {
        default: "bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-[0_4px_14px_rgba(99,102,241,0.35),0_1px_3px_rgba(0,0,0,0.1)] hover:shadow-[0_6px_20px_rgba(99,102,241,0.4),0_2px_6px_rgba(0,0,0,0.1)] hover:from-violet-600 hover:to-indigo-700 border border-violet-600/20",
        destructive: "bg-gradient-to-br from-red-500 to-rose-600 text-white shadow-[0_4px_14px_rgba(239,68,68,0.3)] hover:shadow-[0_6px_20px_rgba(239,68,68,0.4)] hover:from-red-600 hover:to-rose-700",
        outline: "border border-input bg-background/60 backdrop-blur-sm shadow-sm hover:bg-accent hover:text-accent-foreground hover:border-violet-200 dark:hover:border-violet-800/50 hover:shadow-md",
        secondary: "bg-secondary text-secondary-foreground shadow-sm hover:bg-secondary/80 backdrop-blur-sm border border-transparent hover:shadow-md",
        ghost: "hover:bg-accent/70 hover:text-accent-foreground backdrop-blur-sm hover:backdrop-blur-md",
        link: "text-violet-600 underline-offset-4 hover:underline hover:text-violet-700 dark:text-violet-400",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 rounded-lg px-3 text-xs",
        lg: "h-11 rounded-xl px-8 text-base",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => {
    return (
      <button
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    )
  }
)
Button.displayName = "Button"

export { Button, buttonVariants }
