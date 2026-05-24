"use client";

// Trigger affordance for SendStudyDialog. Two visual variants mirror
// StudyExportButton: a 22x22 icon for card-foot toolbars and a
// labelled inline button for the detail-page header.

import { useTranslations } from "next-intl";
import { useState } from "react";

import SendStudyDialog from "@/components/SendStudyDialog";
import SendIcon from "@/components/icons/SendIcon";

interface Props {
  studyId: string;
  patientId: string;
  studyLabel?: string | null;
  variant?: "icon" | "button";
  /** Stop the click event from bubbling up to a parent that owns
   *  selection / navigation (study card grid). */
  stopPropagation?: boolean;
}

export default function SendStudyButton({
  studyId,
  patientId,
  studyLabel,
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
          title={t("title")}
          aria-label={t("title")}
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
          studyId={studyId}
          patientId={patientId}
          studyLabel={studyLabel}
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
        {t("title")}
      </button>
      <SendStudyDialog
        studyId={studyId}
        patientId={patientId}
        studyLabel={studyLabel}
        open={open}
        onClose={() => setOpen(false)}
      />
    </>
  );
}
