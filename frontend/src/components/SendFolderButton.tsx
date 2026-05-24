"use client";

// Trigger affordance for the folder-share variant of SendStudyDialog.
// Twin of SendStudyButton: 22x22 icon for card-foot toolbars, plus a
// labelled inline button for headers. Reuses SendStudyDialog directly
// — the dialog accepts a ``kind`` prop that re-routes the share-create
// call to ``foldersApi.shareLink`` while keeping the form, recipient
// autocomplete, expiry/password/label flow, delivery toggle, and
// post-success panel identical.

import { useTranslations } from "next-intl";
import { useState } from "react";

import SendStudyDialog from "@/components/SendStudyDialog";
import SendIcon from "@/components/icons/SendIcon";

interface Props {
  folderId: string;
  patientId: string;
  folderLabel?: string | null;
  variant?: "icon" | "button";
  stopPropagation?: boolean;
}

export default function SendFolderButton({
  folderId,
  patientId,
  folderLabel,
  variant = "icon",
  stopPropagation = true,
}: Props) {
  const t = useTranslations("sendStudy");
  const [open, setOpen] = useState(false);

  if (variant === "icon") {
    return (
      <>
        <button
          type="button"
          onClick={(e) => {
            if (stopPropagation) e.stopPropagation();
            setOpen(true);
          }}
          title={t("titleFolder")}
          aria-label={t("titleFolder")}
          style={{
            width: 22,
            height: 22,
            padding: 0,
            borderRadius: 6,
            border: "1px solid var(--bv-brand, #e96b1f)",
            background: "var(--bv-card-bg, #fff)",
            color: "var(--bv-brand, #e96b1f)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <SendIcon size={12} />
        </button>
        <SendStudyDialog
          kind="folder"
          studyId={folderId}
          patientId={patientId}
          studyLabel={folderLabel}
          open={open}
          onClose={() => setOpen(false)}
        />
      </>
    );
  }

  return (
    <>
      <button
        type="button"
        className="ghost"
        onClick={(e) => {
          if (stopPropagation) e.stopPropagation();
          setOpen(true);
        }}
      >
        {t("titleFolder")}
      </button>
      <SendStudyDialog
        kind="folder"
        studyId={folderId}
        patientId={patientId}
        studyLabel={folderLabel}
        open={open}
        onClose={() => setOpen(false)}
      />
    </>
  );
}
