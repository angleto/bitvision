"use client";

import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { API_BASE_URL } from "@/lib/api";

interface Props {
  /** Raw bearer token returned once by POST /api/agent-tokens. */
  token: string;
  /** Patient id used to derive a friendly MCP server name. */
  patientId: string;
  /** Optional display name of the patient, used to humanize the server key. */
  patientName?: string | null;
}

export default function McpConfigSnippet({ token, patientId, patientName }: Props) {
  const t = useTranslations("aiShare.snippet");
  const [copiedToken, setCopiedToken] = useState(false);
  const [copiedSnippet, setCopiedSnippet] = useState(false);

  const { serverKey, snippet } = useMemo(() => {
    const short = patientId.replace(/-/g, "").slice(0, 8);
    const slug = (patientName ?? "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 24);
    const key = slug ? `bitvision-${slug}-${short}` : `bitvision-${short}`;
    const config = {
      mcpServers: {
        [key]: {
          command: "uv",
          args: [
            "run",
            "--project",
            "/path/to/bitvision_phoenix/mcp",
            "python",
            "-m",
            "bvmcp.server",
          ],
          env: {
            BVP_MCP_BACKEND_BASE_URL: API_BASE_URL,
            BVP_MCP_AGENT_TOKEN: token,
          },
        },
      },
    };
    return { serverKey: key, snippet: JSON.stringify(config, null, 2) };
  }, [token, patientId, patientName]);

  async function copy(text: string, setFlag: (b: boolean) => void) {
    try {
      await navigator.clipboard.writeText(text);
      setFlag(true);
      setTimeout(() => setFlag(false), 2000);
    } catch {
      /* clipboard may be blocked; ignore */
    }
  }

  return (
    <div>
      <div className="card" style={{ marginBottom: "0.75rem" }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "0.3rem",
          }}
        >
          <strong>{t("tokenLabel")}</strong>
          <button
            type="button"
            className="ghost"
            onClick={() => copy(token, setCopiedToken)}
            style={{ fontSize: "0.85rem" }}
          >
            {copiedToken ? t("copied") : t("copyToken")}
          </button>
        </div>
        <textarea
          readOnly
          value={token}
          rows={2}
          onFocus={(e) => e.currentTarget.select()}
          style={{
            width: "100%",
            fontFamily: "monospace",
            fontSize: "0.8rem",
            resize: "vertical",
          }}
        />
        <p className="meta" style={{ marginTop: "0.3rem", fontSize: "0.8rem", color: "#b45309" }}>
          {t("tokenWarning")}
        </p>
      </div>

      <div className="card">
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "0.3rem",
          }}
        >
          <strong>{t("configLabel", { serverKey })}</strong>
          <button
            type="button"
            className="ghost"
            onClick={() => copy(snippet, setCopiedSnippet)}
            style={{ fontSize: "0.85rem" }}
          >
            {copiedSnippet ? t("copied") : t("copySnippet")}
          </button>
        </div>
        <pre
          style={{
            background: "var(--color-code-bg, #f6f8fa)",
            padding: "0.6rem",
            borderRadius: 4,
            overflow: "auto",
            fontSize: "0.8rem",
            margin: 0,
            maxHeight: 320,
          }}
        >
          {snippet}
        </pre>
        <p
          className="meta"
          style={{ marginTop: "0.4rem", fontSize: "0.8rem" }}
          // biome-ignore lint/security/noDangerouslySetInnerHtml: source is the static i18n bundle, not user input.
          dangerouslySetInnerHTML={{ __html: t.raw("configHint") as string }}
        />
      </div>
    </div>
  );
}
