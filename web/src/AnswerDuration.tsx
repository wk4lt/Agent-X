export function formatDuration(elapsedMs: number): string {
  if (elapsedMs < 60_000) return `${(elapsedMs / 1000).toFixed(1)} 秒`;
  const seconds = Math.floor(elapsedMs / 1000);
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分 ${seconds % 60} 秒`;
}

export function AnswerDuration({ elapsedMs }: { elapsedMs?: number | null }) {
  if (elapsedMs == null || !Number.isFinite(elapsedMs) || elapsedMs < 0) return null;
  return <div className="answer-duration">耗时 {formatDuration(elapsedMs)}</div>;
}
