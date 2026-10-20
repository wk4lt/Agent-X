import { useState } from "react";

type Props = { conversationId: string; sessionId?: string; runId?: string; taskId?: string };

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    // Clipboard API requires a secure context; internal HTTP deployments still need copy.
    const previousFocus = document.activeElement as HTMLElement | null;
    const input = document.createElement("textarea");
    input.value = text;
    input.readOnly = true;
    input.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
    document.body.appendChild(input);
    try {
      input.select();
      if (!document.execCommand("copy")) throw new Error("copy_failed");
    } finally {
      input.remove();
      previousFocus?.focus({ preventScroll: true });
    }
  }
}

export function ConversationIdentifiers({ conversationId, sessionId, runId, taskId }: Props) {
  const [feedback, setFeedback] = useState("");
  const identifiers = [
    { label: "会话记录 ID", name: "conversation_id", value: conversationId },
    { label: "Session ID", name: "session_id", value: sessionId },
    { label: "Run ID", name: "run_id", value: runId },
    { label: "Task ID", name: "task_id", value: taskId },
  ].filter((item): item is { label: string; name: string; value: string } => Boolean(item.value));
  const logDirectory = sessionId ? `LOG_ROOT/${sessionId}/` : undefined;
  const copy = async (value: string, label: string) => {
    try {
      await copyText(value);
      setFeedback(`已复制${label}`);
    } catch {
      setFeedback("复制失败，请选中 ID 手动复制。");
    }
  };
  const primaryId = sessionId || conversationId;
  const primaryLabel = sessionId ? "Session ID" : "会话记录 ID";
  const diagnosticText = [
    ...identifiers.map(({ name, value }) => `${name}=${value}`),
    ...(logDirectory ? [`log_directory=${logDirectory}`] : []),
    ...(sessionId && runId ? [`harness_log=harness_${runId}.jsonl`] : []),
  ].join("\n");

  return <section className="conversation-identifiers" aria-label="会话定位信息">
    <div className="identifier-primary">
      <span>{primaryLabel}</span>
      <code title={primaryId}>{primaryId}</code>
      <button type="button" aria-label={`复制 ${primaryLabel}`} onClick={() => void copy(primaryId, primaryLabel)}>复制</button>
    </div>
    <details className="identifier-details">
      <summary>日志定位</summary>
      <div className="identifier-popover">
        <dl>{identifiers.map(({ label, name, value }) => <div className="identifier-row" key={name}>
          <dt>{label}</dt>
          <dd><code>{value}</code><button type="button" aria-label={`复制 ${name}`} onClick={() => void copy(value, label)}>复制</button></dd>
        </div>)}</dl>
        {logDirectory ? <p className="identifier-log-path">日志目录：<code>{logDirectory}</code><br />
          <code>backend.jsonl</code>{runId && <> · <code>{`harness_${runId}.jsonl`}</code></>}
        </p> : <p className="identifier-log-path">首次提交任务后显示 Session ID 和日志目录。</p>}
        <button type="button" className="copy-diagnostics" onClick={() => void copy(diagnosticText, "定位信息")}>复制全部定位信息</button>
      </div>
    </details>
    <span className="identifier-feedback" role="status">{feedback}</span>
  </section>;
}
