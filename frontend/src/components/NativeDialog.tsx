"use client";

// Reusable wrapper around the HTMLDialogElement.
//
// Why this exists: we had ~10 hand-rolled "fake modal" dialogs that
// each rendered a backdrop ``<div onClick={onClose}>`` + a content
// ``<div onClick={e.stopPropagation()}>``. That pattern is:
//   - keyboard-inaccessible (the backdrop has no Escape; biome
//     surfaces it as ``useKeyWithClickEvents``)
//   - z-index-fragile (sticky site-header overlays the dialog at
//     small viewports)
//   - missing focus trapping and focus restoration
//
// Native ``<dialog>`` + ``showModal()`` solves all three for free:
//   - Escape closes ⇒ ``close`` event ⇒ caller's ``onClose``.
//   - The element is hoisted to the browser top-layer (no z-index
//     gymnastics).
//   - Focus is trapped inside the dialog while open and restored
//     to the previously-focused element on close.
//
// Backdrop click-to-dismiss is wired by listening for clicks on the
// dialog element itself (the backdrop is the dialog area outside the
// content card; clicks there have ``event.target === dialog``).

import { type ReactNode, useEffect, useRef } from "react";

interface Props {
  /** When false, the dialog stays unmounted; when true the wrapper
   *  invokes ``showModal()`` on mount and ``close()`` on unmount. */
  open: boolean;
  /** Called when the user dismisses the dialog (Escape, backdrop
   *  click, or programmatic close). */
  onClose: () => void;
  /** Accessible name. Maps to ``aria-label`` on the <dialog>. */
  ariaLabel?: string;
  /** Optional className on the <dialog>. The library styles the
   *  backdrop via ``::backdrop`` so callers can theme it per
   *  instance (see globals.css). */
  className?: string;
  /** Inline style on the <dialog> itself — typically left empty;
   *  child components style the content card. */
  style?: React.CSSProperties;
  /** Content. Should be a single card-like wrapper; clicks on the
   *  content do NOT close the dialog (the backdrop-click detector
   *  only fires when the click's target is the <dialog> element). */
  children: ReactNode;
}

export default function NativeDialog({
  open,
  onClose,
  ariaLabel,
  className,
  style,
  children,
}: Props) {
  const ref = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dlg = ref.current;
    if (!dlg) return;
    if (open && !dlg.open) dlg.showModal();
    if (!open && dlg.open) dlg.close();
  }, [open]);

  // Close listener: scoped to the open lifecycle, with the latest
  // onClose accessed through a ref so parent re-renders that change
  // the closure don't tear the listener down. Bug history:
  //  * Original: deps were ``[onClose]`` — every parent render with
  //    an inline ``onClose={() => setX(false)}`` lambda re-ran the
  //    effect; its cleanup called ``dlg.close()`` to tidy up, which
  //    fired the native close event, which called the freshly bound
  //    listener, which set ``open`` back to false. Modal auto-
  //    dismissed within milliseconds.
  //  * First fix: deps ``[]`` plus the ref. Avoided the close-loop
  //    but mounted at open=false (the parent gates the dialog with
  //    ``if (!open) return null``) → ref.current was null on the
  //    only effect run → listener never attached on the eventual
  //    open=true transition.
  //  * Now: deps ``[open]``. Listener attaches when the dialog DOM
  //    mounts (open=true), detaches on close/unmount. Calling-side
  //    closures change without affecting the subscription.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const dlg = ref.current;
    if (!dlg) return;
    const onCloseEvent = () => onCloseRef.current();
    dlg.addEventListener("close", onCloseEvent);
    return () => {
      dlg.removeEventListener("close", onCloseEvent);
    };
  }, [open]);

  if (!open) return null;

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: native <dialog> already handles Escape via its built-in ``cancel`` event; the onClick here is the backdrop-click-to-dismiss affordance.
    <dialog
      ref={ref}
      aria-label={ariaLabel}
      className={className}
      onClick={(e) => {
        // Click on the backdrop (i.e. on the <dialog> element itself,
        // not on any nested content card). We do NOT also handle
        // ``onMouseDown`` here — text-selection inside the dialog
        // that ends with mouse-up over the backdrop should not close.
        if (e.target === e.currentTarget) onClose();
      }}
      style={{
        padding: 0,
        margin: 0,
        border: "none",
        background: "transparent",
        // Default to centered card layout; callers can override.
        ...style,
      }}
    >
      {children}
    </dialog>
  );
}
