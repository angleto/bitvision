"use client";

/**
 * Minimal PDF viewer — relies on the browser's built-in PDF plugin rather
 * than bundling pdf.js. Chromium and Firefox both render application/pdf
 * natively inside an iframe; for other user-agents (rare on desktop) the
 * browser falls back to a download prompt and DocumentPreview catches
 * that via the onError signal.
 */
export default function PDFViewer({ url }: { url: string }) {
  return (
    <iframe
      src={url}
      title="document preview"
      style={{ width: "100%", height: "100%", border: 0 }}
    />
  );
}
