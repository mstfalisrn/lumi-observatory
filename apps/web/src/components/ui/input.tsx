import * as React from "react"
import { cn } from "@/lib/utils"

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          "flex h-10 w-full rounded-xl border border-input bg-background/60 backdrop-blur-sm px-3.5 py-2 text-sm shadow-sm transition-all duration-200 ease-[cubic-bezier(0.32,0.72,0,1)] file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground/60 hover:border-violet-200 focus-visible:outline-none focus-visible:border-violet-300 focus-visible:ring-2 focus-visible:ring-violet-500/20 focus-visible:bg-background disabled:cursor-not-allowed disabled:opacity-50 md:text-sm dark:bg-white/[0.04] dark:hover:border-white/10 dark:focus-visible:border-violet-500/40 dark:focus-visible:bg-white/[0.06]",
          className
        )}
        ref={ref}
        {...props}
      />
    )
  }
)
Input.displayName = "Input"

export { Input }
