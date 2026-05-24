"use client";

// In-app modal host. Replaces the native ``window.confirm`` /
// ``window.prompt`` browser dialogs (the ones that render with the
// ugly "localhost:3000 says" prefix and don't theme).
//
// Usage at the call site:
//
//   const { confirm, prompt } = useModal();
//
//   if (await confirm({ message: t("areYouSure"), destructive: true })) {
//     await deleteThing();
//   }
//
//   const text = await prompt({ label: "Annotation text", defaultValue: "Note" });
//   if (text != null) saveAnnotation(text);
//
// Both functions return a Promise; the caller can ``await`` it
// without juggling open/close state. The provider is mounted once
// in the root layout and serialises one dialog at a time (a second
// open call replaces the first; this matches the implicit semantics
// of the native APIs).

import { useTranslations } from "next-intl";
import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

interface ConfirmOptions {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Style the confirm button as destructive (red). */
  destructive?: boolean;
}

interface PromptOptions {
  title?: string;
  message?: string;
  label?: string;
  defaultValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** When true, render a multi-line ``<textarea>`` instead of a single
   * line input. Useful for free-text annotations. */
  multiline?: boolean;
}

interface AlertOptions {
  title?: string;
  message: string;
  /** Render the message in a danger style — used for download / save
   * failures so the dim-red banner makes the failure obvious without
   * needing a user-typed acknowledgement. */
  variant?: "info" | "danger";
  confirmLabel?: string;
}

type DialogState =
  | {
      kind: "confirm";
      opts: ConfirmOptions;
      resolve: (v: boolean) => void;
    }
  | {
      kind: "prompt";
      opts: PromptOptions;
      resolve: (v: string | null) => void;
    }
  | {
      kind: "alert";
      opts: AlertOptions;
      resolve: () => void;
    }
  | null;

interface ModalApi {
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
  prompt: (opts: PromptOptions) => Promise<string | null>;
  /** In-app replacement for ``window.alert``: a single-button
   *  informational dialog. Themed, escape-dismissible, focus-restoring;
   *  use this for non-blocking user-error messages (download failure,
   *  validation hint) instead of the native blocking alert. */
  alert: (opts: AlertOptions) => Promise<void>;
}

const ModalContext = createContext<ModalApi | null>(null);

export function ModalProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DialogState>(null);

  const confirm = useCallback((opts: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      setState({ kind: "confirm", opts, resolve });
    });
  }, []);

  const prompt = useCallback((opts: PromptOptions) => {
    return new Promise<string | null>((resolve) => {
      setState({ kind: "prompt", opts, resolve });
    });
  }, []);

  const alert = useCallback((opts: AlertOptions) => {
    return new Promise<void>((resolve) => {
      setState({ kind: "alert", opts, resolve });
    });
  }, []);

  const api = useMemo<ModalApi>(() => ({ confirm, prompt, alert }), [confirm, prompt, alert]);

  // Resolve outstanding promise when the dialog closes for any
  // reason (button, escape, backdrop). We always wrap setState to
  // null after resolving so the next open call gets a fresh state.
  const close = useCallback((value: boolean | string | null) => {
    setState((current) => {
      if (current) {
        if (current.kind === "confirm") {
          current.resolve(typeof value === "boolean" ? value : false);
        } else if (current.kind === "prompt") {
          current.resolve(typeof value === "string" ? value : null);
        } else {
          // alert: single-button, no return value
          current.resolve();
        }
      }
      return null;
    });
  }, []);

  return (
    <ModalContext.Provider value={api}>
      {children}
      {state && <DialogRenderer state={state} onClose={close} />}
    </ModalContext.Provider>
  );
}

export function useModal(): ModalApi {
  const ctx = useContext(ModalContext);
  if (ctx === null) {
    throw new Error("useModal must be used within <ModalProvider>");
  }
  return ctx;
}

function DialogRenderer({
  state,
  onClose,
}: {
  state: NonNullable<DialogState>;
  onClose: (value: boolean | string | null) => void;
}) {
  const t = useTranslations("modal");
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const initialFocusedRef = useRef<HTMLElement | null>(null);
  const [value, setValue] = useState<string>(
    state.kind === "prompt" ? (state.opts.defaultValue ?? "") : "",
  );

  // Capture the previously-focused element to restore on close — keeps
  // keyboard users on their original control after the dialog closes.
  useEffect(() => {
    initialFocusedRef.current = (document.activeElement as HTMLElement) ?? null;
    return () => {
      initialFocusedRef.current?.focus?.();
    };
  }, []);

  // Focus the input on mount for prompts, the confirm button for
  // confirms. Select-all on the prompt so a default value can be
  // overwritten with one keystroke.
  useEffect(() => {
    const target =
      state.kind === "prompt"
        ? inputRef.current
        : (cardRef.current?.querySelector(
            'button[data-action="confirm"]',
          ) as HTMLButtonElement | null);
    target?.focus();
    if (state.kind === "prompt" && inputRef.current && "select" in inputRef.current) {
      (inputRef.current as HTMLInputElement).select?.();
    }
  }, [state.kind]);

  // Esc cancels regardless of focus. For ``alert`` Esc closes (the
  // user has acknowledged), same as clicking the OK button — alert
  // has no failure path.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        if (state.kind === "confirm") onClose(false);
        else if (state.kind === "alert") onClose(true);
        else onClose(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, state.kind]);

  const isPrompt = state.kind === "prompt";
  const isAlert = state.kind === "alert";
  const opts = state.opts;
  const titleId = "bvp-modal-title";

  function handleConfirm() {
    if (isAlert) onClose(true);
    else onClose(isPrompt ? value : true);
  }
  function handleCancel() {
    onClose(isPrompt ? null : false);
  }

  const confirmLabel = opts.confirmLabel ?? t("ok");
  const cancelLabel = "cancelLabel" in opts ? (opts.cancelLabel ?? t("cancel")) : t("cancel");
  const destructive =
    (state.kind === "confirm" && state.opts.destructive) ||
    (state.kind === "alert" && state.opts.variant === "danger");

  return (
    <div
      role="presentation"
      onMouseDown={(e) => {
        // Click on backdrop (not on the card) cancels.
        if (e.target === e.currentTarget) handleCancel();
      }}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        backdropFilter: "blur(2px)",
        zIndex: 1000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "1rem",
      }}
    >
      <div
        ref={cardRef}
        // biome-ignore lint/a11y/useSemanticElements: ModalHost is the custom modal infrastructure — converting to native <dialog> requires reworking the show/close imperative flow which is out of scope for this batch.
        role="dialog"
        aria-modal="true"
        aria-labelledby={opts.title ? titleId : undefined}
        style={{
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, inherit)",
          border: "1px solid var(--bv-card-border, #d0d5dd)",
          borderRadius: 10,
          boxShadow: "0 18px 42px rgba(0,0,0,0.35)",
          minWidth: 320,
          maxWidth: 540,
          width: "min(100%, 540px)",
          padding: "1.1rem 1.25rem",
        }}
      >
        {opts.title && (
          <h3 id={titleId} style={{ margin: "0 0 0.5rem", fontSize: "1rem" }}>
            {opts.title}
          </h3>
        )}
        {state.kind === "confirm" && (
          <p style={{ margin: "0 0 1rem", fontSize: "0.92rem", lineHeight: 1.45 }}>
            {state.opts.message}
          </p>
        )}
        {state.kind === "alert" && (
          <p
            role={state.opts.variant === "danger" ? "alert" : undefined}
            style={{
              margin: "0 0 1rem",
              fontSize: "0.92rem",
              lineHeight: 1.45,
              color:
                state.opts.variant === "danger"
                  ? "var(--bv-danger, #b91c1c)"
                  : "var(--bv-fg, inherit)",
            }}
          >
            {state.opts.message}
          </p>
        )}
        {state.kind === "prompt" && (
          <>
            {state.opts.message && (
              <p style={{ margin: "0 0 0.6rem", fontSize: "0.9rem", lineHeight: 1.45 }}>
                {state.opts.message}
              </p>
            )}
            {/* htmlFor explicitly associates the label with the
                input (or textarea) below. The previous structure
                wrapped both branches of a ternary inside the label,
                which biome's noLabelWithoutControl can't statically
                resolve to a control child. */}
            {state.opts.label && (
              <label
                htmlFor="bvp-modal-input"
                className="meta"
                style={{ fontSize: "0.75rem", display: "block", marginBottom: "0.3rem" }}
              >
                {state.opts.label}
              </label>
            )}
            {state.opts.multiline ? (
              <textarea
                id="bvp-modal-input"
                ref={(el) => {
                  inputRef.current = el;
                }}
                value={value}
                rows={4}
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                    e.preventDefault();
                    handleConfirm();
                  }
                }}
                placeholder={state.opts.placeholder}
                style={inputStyle}
              />
            ) : (
              <input
                id="bvp-modal-input"
                ref={(el) => {
                  inputRef.current = el;
                }}
                type="text"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleConfirm();
                  }
                }}
                placeholder={state.opts.placeholder}
                style={inputStyle}
              />
            )}
          </>
        )}
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "1rem",
          }}
        >
          {!isAlert && (
            <button type="button" className="ghost" onClick={handleCancel}>
              {cancelLabel}
            </button>
          )}
          <button
            type="button"
            data-action="confirm"
            onClick={handleConfirm}
            style={
              destructive
                ? {
                    background: "var(--bv-danger, #d6322e)",
                    color: "#fff",
                    border: "1px solid var(--bv-danger, #d6322e)",
                  }
                : undefined
            }
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "0.5rem 0.65rem",
  border: "1px solid var(--bv-card-border, #d0d5dd)",
  borderRadius: 6,
  background: "var(--bv-card-bg, #fff)",
  color: "var(--bv-fg, inherit)",
  fontFamily: "inherit",
  fontSize: "0.92rem",
  resize: "vertical",
};
