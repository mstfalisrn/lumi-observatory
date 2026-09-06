import * as React from "react"
import { createPortal } from "react-dom"
import { motion, AnimatePresence } from "framer-motion"
import { X } from "lucide-react"
import { cn } from "@/lib/utils"

type SheetContextValue = {
  open: boolean
  onOpenChange: (open: boolean) => void
}

const SheetContext = React.createContext<SheetContextValue | null>(null)

function useSheetContext() {
  const ctx = React.useContext(SheetContext)
  if (!ctx) throw new Error("Sheet components must be inside <Sheet>")
  return ctx
}

export function Sheet({
  open: controlledOpen,
  onOpenChange,
  defaultOpen = false,
  children,
}: {
  open?: boolean
  onOpenChange?: (open: boolean) => void
  defaultOpen?: boolean
  children: React.ReactNode
}) {
  const [internalOpen, setInternalOpen] = React.useState(defaultOpen)
  const open = controlledOpen ?? internalOpen
  const setOpen = React.useCallback(
    (v: boolean) => {
      if (controlledOpen === undefined) setInternalOpen(v)
      onOpenChange?.(v)
    },
    [controlledOpen, onOpenChange]
  )
  return (
    <SheetContext.Provider value={{ open, onOpenChange: setOpen }}>
      {children}
    </SheetContext.Provider>
  )
}

export function SheetTrigger({
  children,
  asChild,
  ...props
}: {
  children: React.ReactElement
  asChild?: boolean
} & React.HTMLAttributes<HTMLElement>) {
  const { onOpenChange } = useSheetContext()
  // asChild true still clones child; asChild false wraps in button (kept same for simplicity portal-wise)
  return React.cloneElement(children as React.ReactElement<any>, {
    onClick: (e: React.MouseEvent) => {
      ;(children.props as any)?.onClick?.(e)
      ;(props as any)?.onClick?.(e)
      onOpenChange(true)
    },
  })
}

export function SheetOverlay({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  const { open, onOpenChange } = useSheetContext()
  if (!open) return null
  return createPortal(
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.15 }}
      className={cn("fixed inset-0 z-50 bg-black/50 backdrop-blur-sm", className)}
      onClick={() => onOpenChange(false)}
      {...(props as any)}
    />,
    document.body
  )
}

export function SheetContent({
  children,
  side = "left",
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & {
  side?: "left" | "right" | "top" | "bottom"
}) {
  const { open, onOpenChange } = useSheetContext()

  React.useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false)
    }
    document.addEventListener("keydown", onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = "hidden"
    return () => {
      document.removeEventListener("keydown", onKey)
      document.body.style.overflow = prev
    }
  }, [open, onOpenChange])

  const sideClasses: Record<string, string> = {
    left: "inset-y-0 left-0 h-full w-[300px] border-r sm:max-w-sm",
    right: "inset-y-0 right-0 h-full w-[300px] border-l sm:max-w-sm",
    top: "inset-x-0 top-0 border-b",
    bottom: "inset-x-0 bottom-0 border-t",
  }

  const variants: Record<string, any> = {
    left: { initial: { x: "-100%" }, animate: { x: 0 }, exit: { x: "-100%" } },
    right: { initial: { x: "100%" }, animate: { x: 0 }, exit: { x: "100%" } },
    top: { initial: { y: "-100%" }, animate: { y: 0 }, exit: { y: "-100%" } },
    bottom: { initial: { y: "100%" }, animate: { y: 0 }, exit: { y: "100%" } },
  }

  return createPortal(
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 z-50 bg-black/40 backdrop-blur-md"
            onClick={() => onOpenChange(false)}
            aria-hidden
          />
          <motion.div
            initial={variants[side].initial}
            animate={variants[side].animate}
            exit={variants[side].exit}
            transition={{ duration: 0.24, ease: [0.32, 0.72, 0, 1] }}
            className={cn(
              "fixed z-50 bg-background/85 backdrop-blur-2xl supports-[backdrop-filter]:bg-background/75 shadow-[0_16px_48px_rgba(0,0,0,0.22),0_4px_16px_rgba(0,0,0,0.12)] flex flex-col border-white/10",
              sideClasses[side],
              className
            )}
            role="dialog"
            aria-modal="true"
            {...(props as any)}
          >
            <button
              onClick={() => onOpenChange(false)}
              className="absolute right-4 top-4 rounded-sm opacity-70 ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
            <div className="flex-1 overflow-y-auto p-6 pt-10">{children}</div>
          </motion.div>
        </>
      )}
    </AnimatePresence>,
    document.body
  )
}

export function SheetHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col space-y-2 text-center sm:text-left", className)} {...props} />
}

export function SheetTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h3 className={cn("text-lg font-semibold text-foreground", className)} {...props} />
}

export function SheetDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-sm text-muted-foreground", className)} {...props} />
}

export function SheetFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col-reverse sm:flex-row sm:justify-end sm:space-x-2", className)} {...props} />
}

export function SheetClose({
  children,
  ...props
}: { children: React.ReactElement } & React.HTMLAttributes<HTMLElement>) {
  const { onOpenChange } = useSheetContext()
  return React.cloneElement(children as React.ReactElement<any>, {
    onClick: (e: React.MouseEvent) => {
      ;(children.props as any)?.onClick?.(e)
      ;(props as any)?.onClick?.(e)
      onOpenChange(false)
    },
  })
}
