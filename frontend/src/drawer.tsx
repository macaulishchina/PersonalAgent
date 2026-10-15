import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

type Frame = { opacity: string; transform: string; scrim: string };

export function Drawer({
  open,
  onClose,
  id,
  label,
  children,
}: {
  open: boolean;
  onClose: () => void;
  id: string;
  label: string;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const scrim = useRef<HTMLDivElement>(null);
  const interrupted = useRef<Frame | null>(null);
  const query = useRef(window.matchMedia("(max-width: 600px)"));
  const [mobile, setMobile] = useState(query.current.matches);
  const shownModal = useRef<boolean | null>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const media = query.current;
    const change = () => setMobile(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    if (!open || mobile) return;
    const outside = (event: PointerEvent) => {
      const target = event.target as Element;
      if (
        event.button !== 0 ||
        panel.current?.contains(target) ||
        target.closest(".composer-shortcuts")
      )
        return;
      close.current();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) {
        event.preventDefault();
        close.current();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open, mobile]);
  const scrollLock = useRef<{ body: string; html: string } | null>(null);
  const restoreScroll = () => {
    if (!scrollLock.current) return;
    document.body.style.overflow = scrollLock.current.body;
    document.documentElement.style.overflow = scrollLock.current.html;
    scrollLock.current = null;
  };
  useLayoutEffect(() => {
    const element = dialog.current!;
    if (!open && !element.open) return;
    if (open && element.open && shownModal.current !== mobile) {
      // A breakpoint change must update modality without leaving stale inertness
      // or scroll locks behind. Preserve the visible frame through that change.
      const style = getComputedStyle(panel.current!);
      interrupted.current = {
        opacity: style.opacity,
        transform: style.transform,
        scrim: getComputedStyle(scrim.current!).opacity,
      };
      element.close();
      restoreScroll();
    }
    if (open && !element.open) {
      if (mobile) {
        scrollLock.current = {
          body: document.body.style.overflow,
          html: document.documentElement.style.overflow,
        };
        document.body.style.overflow = "hidden";
        document.documentElement.style.overflow = "hidden";
        element.showModal();
      } else {
        element.show();
      }
      shownModal.current = mobile;
      element
        .querySelector<HTMLButtonElement>(
          ".activity-page:not([hidden]) [data-panel-close]",
        )
        ?.focus({ preventScroll: true });
    }
    const surface = panel.current!;
    const shade = scrim.current!;
    const from = interrupted.current ?? {
      opacity: open ? "0" : "1",
      transform: open ? "translateY(8px)" : "translateY(0px)",
      scrim: open ? "0" : "1",
    };
    interrupted.current = null;
    let current = true;
    const finish = () => {
      if (!current) return;
      if (!open) {
        element.close();
        shownModal.current = null;
        restoreScroll();
      }
    };
    const reduced = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    if (reduced || !surface.animate) {
      finish();
      return;
    }
    // Apply the first frame before paint. Only the surface and shade animate;
    // the modal itself and its content geometry stay stable on every viewport.
    const options = {
      duration: open ? 180 : 140,
      easing: "cubic-bezier(0.2, 0, 0, 1)",
      fill: "both" as const,
    };
    const animations = [
      surface.animate(
        [
          { opacity: from.opacity, transform: from.transform },
          {
            opacity: open ? 1 : 0,
            transform: open ? "translateY(0px)" : "translateY(8px)",
          },
        ],
        options,
      ),
      shade.animate(
        [{ opacity: from.scrim }, { opacity: open ? 1 : 0 }],
        options,
      ),
    ];
    void Promise.allSettled(
      animations.map((animation) => animation.finished),
    ).then(() => {
      if (!current) return;
      finish();
      animations.forEach((animation) => animation.cancel());
    });
    return () => {
      current = false;
      if (
        element.open &&
        animations.some((animation) => animation.playState === "running")
      ) {
        const style = getComputedStyle(surface);
        interrupted.current = {
          opacity: style.opacity,
          transform: style.transform,
          scrim: getComputedStyle(shade).opacity,
        };
      }
      animations.forEach((animation) => animation.cancel());
    };
  }, [open, mobile]);
  useLayoutEffect(
    () => () => {
      dialog.current?.close();
      restoreScroll();
    },
    [],
  );
  return (
    <dialog
      ref={dialog}
      id={id}
      className="task-layer"
      aria-label={label}
      aria-modal={mobile || undefined}
      data-state={open ? "open" : "closing"}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (
          event.target === event.currentTarget ||
          event.target === scrim.current
        )
          onClose();
      }}
    >
      <div className="task-scrim" ref={scrim} aria-hidden="true" />
      <div className="task-panel" ref={panel}>
        <div className="task-sheet-handle" aria-hidden="true" />
        {children}
      </div>
    </dialog>
  );
}
