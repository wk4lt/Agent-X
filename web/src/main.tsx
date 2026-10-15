import { ChangeEvent, FormEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { MarkdownMessage } from "./MarkdownMessage";
import { SkillHover } from "./SkillHover";

type Run = { run_id: string; status: string; stop_reason?: string; failure_stage?: string; failure_code?: string; failure_message?: string; final_answer?: string };
type Task = { task_id: string; input: string; project_id: string; conversation_id?: string; run?: Run };
type Conversation = { id: string; title: string; created_at: string; updated_at: string };
type Message = { id: string; sequence: number; role: "user" | "assistant" | "tool"; content: string; status: string; tool_calls?: unknown[] };
type Skill = { name: string; description: string; source: string; trust_level: string };
type WorkspaceFile = { name: string; path: string; size_bytes: number; modified_at: string };
type Envelope = { sequence: number; type: string; payload: Record<string, unknown> };
type TokenStatus = { estimated?: string; actual?: string; compaction?: string };
type ProgressItem = { sequence: number; label: string };

const terminalEvents = new Set(["run.completed", "run.failed", "run.cancelled", "run.interrupted"]);
const eventNames = ["run.accepted", "run.started", "run.progress", "context.built", "context.compaction.started", "context.compaction.completed", "context.compaction.failed", "skill.discovered", "skill.loaded", "skill.load_denied", "skill.invalid", "skill.script.approval_required", "skill.script.started", "skill.script.stdout", "skill.script.completed", "skill.script.failed", "assistant.delta", "assistant.message", "tool.started", "tool.completed", "tool.failed", "usage.updated", ...terminalEvents];
const runStatusLabels: Record<string, string> = {
  accepted: "等待运行",
  running: "正在思考",
  waiting_for_user: "等待输入",
  completed: "已完成",
  failed: "运行失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

function shortTitle(input: string) { return input.replace(/\s+/g, " ").trim().slice(0, 42) || "新会话"; }
function formatSize(bytes: number) { return bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`; }
function Icon({ children }: { children: string }) { return <span className="icon" aria-hidden>{children}</span>; }

function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversation, setActiveConversation] = useState<Conversation>();
  const [messages, setMessages] = useState<Message[]>([]);
  const [activeTask, setActiveTask] = useState<Task>();
  const [draft, setDraft] = useState("");
  const [answer, setAnswer] = useState("");
  const [activity, setActivity] = useState("");
  const [progress, setProgress] = useState<ProgressItem[]>([]);
  const [error, setError] = useState("");
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [isSending, setIsSending] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [tokens, setTokens] = useState<TokenStatus>({});
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [skillMenuOpen, setSkillMenuOpen] = useState(false);
  const stream = useRef<EventSource | null>(null);
  const uploadInput = useRef<HTMLInputElement | null>(null);
  const draftInput = useRef<HTMLTextAreaElement | null>(null);

  useLayoutEffect(() => {
    const input = draftInput.current;
    if (!input) return;

    const maxHeight = 170;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, maxHeight)}px`;
    input.style.overflowY = input.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [draft]);

  useEffect(() => {
    void loadConversations();
    void fetch("/api/skills").then((response) => response.ok ? response.json() : []).then((data: Skill[]) => setSkills(data)).catch(() => undefined);
    return () => stream.current?.close();
  }, []);

  const loadConversations = async () => {
    const response = await fetch("/api/conversations");
    if (response.ok) setConversations((await response.json() as { items: Conversation[] }).items);
    else setError("无法加载历史会话。");
  };

  const loadFiles = async (taskId: string) => {
    const response = await fetch(`/api/tasks/${taskId}/workspace/files`);
    if (response.ok) setFiles(await response.json() as WorkspaceFile[]);
  };
  const applyEvent = (taskId: string, event: Envelope) => {
    const round = Number(event.payload.round ?? 0);
    const roundLabel = (label: string) => round > 0 ? `第 ${round} 轮 · ${label}` : label;
    const showProgress = (label: string) => {
      const next = roundLabel(label);
      setActivity(next);
      setProgress((old) => [...old.filter((item) => item.sequence !== event.sequence), { sequence: event.sequence, label: next }].slice(-5));
    };
    if (event.type === "assistant.delta") setAnswer((old) => old + String(event.payload.delta ?? ""));
    if (event.type === "assistant.message") setAnswer(String(event.payload.content ?? ""));
    if (event.type === "run.progress") showProgress(String(event.payload.message ?? "正在处理"));
    if (event.type === "tool.started") showProgress(`正在使用工具：${String(event.payload.name ?? "未知工具")}`);
    if (event.type === "tool.completed") showProgress(`工具已完成：${String(event.payload.name ?? "未知工具")}`);
    if (event.type === "tool.failed") showProgress(`工具执行失败：${String(event.payload.name ?? "未知工具")}`);
    if (event.type === "skill.loaded") showProgress(`已加载 Skill：${String(event.payload.name ?? "")}`);
    if (event.type === "skill.script.approval_required") showProgress("Skill 脚本等待授权");
    if (event.type === "skill.script.started") showProgress(`正在运行 Skill 脚本：${String(event.payload.script ?? "")}`);
    if (event.type === "skill.script.completed") showProgress("Skill 脚本已完成");
    if (event.type === "skill.script.failed") showProgress("Skill 脚本未完成");
    if (event.type === "run.started") {
      showProgress("运行已开始");
      setActiveTask((old) => old?.task_id === taskId && old.run ? { ...old, run: { ...old.run, status: "running" } } : old);
    }
    if (event.type === "context.built") {
      if (event.payload.status === "ready") {
        setTokens((old) => ({ ...old, estimated: `${event.payload.input_tokens_after_est ?? 0} / ${event.payload.available_input_tokens ?? 0} 估算输入 Token`, compaction: undefined }));
      } else setActivity("上下文预算不足");
    }
    if (event.type === "context.compaction.started") { showProgress("正在压缩较早的历史"); setTokens((old) => ({ ...old, compaction: "历史压缩中" })); }
    if (event.type === "context.compaction.completed") { showProgress("历史压缩已完成"); setTokens((old) => ({ ...old, compaction: "历史已压缩" })); }
    if (event.type === "context.compaction.failed") { showProgress("历史压缩失败，已降级裁剪"); setTokens((old) => ({ ...old, compaction: "历史压缩失败，已降级裁剪" })); }
    if (event.type === "usage.updated" && event.payload.kind === "run") {
      const input = event.payload.prompt_tokens_actual ?? "—", output = event.payload.completion_tokens_actual ?? "—";
      setTokens((old) => ({ ...old, actual: `实际：输入 ${input} · 输出 ${output}` }));
    }
    if (terminalEvents.has(event.type)) {
      stream.current?.close();
      const status = event.type.slice(4), stopReason = String(event.payload.stop_reason ?? "");
      setActivity(status === "completed" ? "已完成" : `已停止：${stopReason || status}`);
      const failure = event.type === "run.failed" ? {
        failure_stage: String(event.payload.stage ?? ""), failure_code: String(event.payload.error_code ?? ""),
        failure_message: String(event.payload.error_message ?? ""),
      } : {};
      setActiveTask((old) => old?.task_id === taskId && old.run ? { ...old, run: { ...old.run, status, stop_reason: stopReason, ...failure } } : old);
      setTasks((old) => old.map((task) => task.task_id === taskId && task.run ? { ...task, run: { ...task.run, status, stop_reason: stopReason, ...failure } } : task));
      void loadConversationMessages(activeConversation?.id);
      void loadConversations();
    }
  };
  const connect = (taskId: string) => {
    stream.current?.close();
    const source = new EventSource(`/api/tasks/${taskId}/events`); stream.current = source;
    eventNames.forEach((name) => source.addEventListener(name, (raw) => { try { applyEvent(taskId, JSON.parse((raw as MessageEvent<string>).data) as Envelope); } catch { setError("无法解析运行事件。"); } }));
    source.onerror = () => { if (source.readyState !== EventSource.CLOSED) setError("连接暂时中断，正在重连…"); };
  };
  const selectTask = async (task: Task) => {
    stream.current?.close(); setActiveTask(task); setAnswer(""); setActivity(""); setProgress([]); setError(""); setFiles([]); setTokens({});
    const response = await fetch(`/api/tasks/${task.task_id}`);
    if (!response.ok) { setError("无法读取该会话。"); return; }
    const current = await response.json() as Task;
    setActiveTask(current); setTasks((old) => old.map((item) => item.task_id === current.task_id ? current : item));
    if (current.run?.final_answer) setAnswer(current.run.final_answer);
    if (current.run && !["completed", "failed", "cancelled", "interrupted"].includes(current.run.status)) connect(current.task_id);
    await loadFiles(current.task_id);
  };
  const loadConversationMessages = async (conversationId?: string) => {
    if (!conversationId) return;
    const response = await fetch(`/api/conversations/${conversationId}/messages`);
    if (response.ok) { setMessages((await response.json() as { items: Message[] }).items); setAnswer(""); }
  };
  const selectConversation = async (conversation: Conversation) => {
    stream.current?.close(); setActiveConversation(conversation); setActiveTask(undefined); setAnswer(""); setActivity(""); setProgress([]); setError(""); setFiles([]); setTokens({});
    await loadConversationMessages(conversation.id);
    const response = await fetch(`/api/conversations/${conversation.id}/latest-task`);
    if (response.ok) { const task = await response.json() as Task | null; if (task) { setActiveTask(task); await loadFiles(task.task_id); if (task.run && !["completed", "failed", "cancelled", "interrupted"].includes(task.run.status)) connect(task.task_id); } }
  };
  const newSession = async () => {
    stream.current?.close(); setActiveTask(undefined); setDraft(""); setAnswer(""); setActivity(""); setProgress([]); setError(""); setFiles([]); setTokens({}); setMessages([]);
    const response = await fetch("/api/conversations", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({}) });
    if (!response.ok) { setError("无法新建会话。"); return; }
    const conversation = await response.json() as Conversation; setActiveConversation(conversation); await loadConversations();
  };
  async function submit(event: FormEvent) {
    event.preventDefault(); const input = draft.trim(); if (!input || isSending) return;
    setIsSending(true); setError(""); setAnswer(""); setActivity("正在创建任务…"); setProgress([{ sequence: 0, label: "正在创建任务…" }]); setTokens({});
    try {
      let conversation = activeConversation;
      if (!conversation) { const createdConversation = await fetch("/api/conversations", { method: "POST" }); if (!createdConversation.ok) throw new Error("创建会话失败"); conversation = await createdConversation.json() as Conversation; setActiveConversation(conversation); }
      const response = await fetch("/api/tasks", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ input, conversation_id: conversation.id, selected_skills: selectedSkills }) });
      if (!response.ok) throw new Error(`创建失败 (${response.status})`);
      const created = await response.json() as Task;
      setTasks((old) => [created, ...old.filter((task) => task.task_id !== created.task_id)]);
      setActiveTask(created); setMessages((old) => [...old, { id: `optimistic-${created.task_id}`, sequence: -1, role: "user", content: input, status: "completed" }]); setDraft(""); setFiles([]); connect(created.task_id); await loadConversations();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "创建会话失败。"); setActivity(""); } finally { setIsSending(false); }
  }
  async function cancel() { if (activeTask) await fetch(`/api/tasks/${activeTask.task_id}/cancel`, { method: "POST" }); }
  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0]; event.target.value = ""; if (!selected || !activeTask) return;
    setIsUploading(true); setError(""); const body = new FormData(); body.append("file", selected);
    const response = await fetch(`/api/tasks/${activeTask.task_id}/workspace/files`, { method: "POST", body }); setIsUploading(false);
    if (!response.ok) { setError(response.status === 409 ? "同名文件已存在。" : "文件上传失败。"); return; }
    await loadFiles(activeTask.task_id);
  }

  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark">AX</span><span>Agent-X</span></div><button className="new-chat" onClick={() => void newSession()}><Icon>＋</Icon> 新建会话</button>
      <nav className="session-list" aria-label="会话列表"><p className="section-label">历史会话</p>{conversations.length === 0 && <p className="empty-list">尚未创建会话</p>}{conversations.map((conversation) => <div className={`session-row ${activeConversation?.id === conversation.id ? "selected" : ""}`} key={conversation.id}><button className="session-item" onClick={() => void selectConversation(conversation)}><Icon>◌</Icon><span>{conversation.title}</span></button><button className="delete-chat" aria-label="删除会话" onClick={async () => { if (!window.confirm("删除此会话？")) return; await fetch(`/api/conversations/${conversation.id}`, { method: "DELETE" }); if (activeConversation?.id === conversation.id) { setActiveConversation(undefined); setActiveTask(undefined); setMessages([]); } await loadConversations(); }}>×</button></div>)}</nav><div className="sidebar-footer"><span className="avatar">AX</span><span>本地开发环境</span></div></aside>
    <main className="chat-area"><header className="chat-header"><div><strong>{activeConversation?.title || "新会话"}</strong><span className="model-chip">DeepSeek</span>{(tokens.estimated || tokens.actual || tokens.compaction) && <span className="token-status">{tokens.estimated}{tokens.actual && <> · {tokens.actual}</>}{tokens.compaction && <> · {tokens.compaction}</>}</span>}</div>{activeTask?.run && <span className={`status status-${activeTask.run.status}`}>{runStatusLabels[activeTask.run.status] || activeTask.run.status}</span>}</header>
      <div className="conversation">{!activeConversation && <div className="welcome"><div className="welcome-mark">AX</div><h1>今天想完成什么？</h1><p>Agent-X 会以流式方式协助你完成任务；历史会话与工作区仅保存在当前匿名身份下。</p></div>}{activeConversation && <>{messages.filter((message) => message.role !== "tool" && !(message.role === "assistant" && (!message.content.trim() || message.tool_calls?.length))).map((message) => <article key={message.id} className={`message ${message.role === "user" ? "user-message" : "assistant-message"}`}><div className={`message-avatar ${message.role === "assistant" ? "assistant-avatar" : ""}`}>{message.role === "user" ? "你" : "AX"}</div><div className="answer">{message.role === "assistant" ? <MarkdownMessage content={message.content} /> : message.content}</div></article>)}{activeTask?.run && ["accepted", "running"].includes(activeTask.run.status) && <article className="message assistant-message progress-message"><div className="message-avatar assistant-avatar">AX</div><div className="thinking-card"><span className="typing">{activity || "正在准备回答…"}</span>{progress.length > 1 && <ol className="progress-list">{progress.slice(0, -1).map((item) => <li key={item.sequence}>{item.label}</li>)}</ol>}</div></article>}{activeTask && answer && <article className="message assistant-message"><div className="message-avatar assistant-avatar">AX</div><div className="answer"><MarkdownMessage content={answer} /></div></article>}{activeTask?.run?.status === "failed" && <article className="message assistant-message"><div className="message-avatar assistant-avatar">AX</div><div className="answer failure-detail">运行失败：{activeTask.run.stop_reason || "未知原因"}{activeTask.run.failure_stage && <>（阶段：{activeTask.run.failure_stage}）</>}{activeTask.run.failure_code && <><br />错误类型：{activeTask.run.failure_code}</>}{activeTask.run.failure_message && <><br />错误信息：{activeTask.run.failure_message}</>}</div></article>}</>}{error && <p className="notice error-notice">{error}</p>}</div>
      <div className="composer-wrap"><div className="skill-tray">{selectedSkills.map((name) => <SkillHover name={name} chip key={name}><span className="skill-chip">✦ {name}<button aria-label={`移除 ${name}`} onClick={() => setSelectedSkills((old) => old.filter((item) => item !== name))}>×</button></span></SkillHover>)}</div><form className="composer" onSubmit={submit}><button className="skill-picker" type="button" title="选择 Skill" onClick={() => setSkillMenuOpen((open) => !open)}>✦ <span>Skills</span></button><textarea ref={draftInput} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="给 Agent-X 发送消息" rows={1} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} /><button className="send" type="submit" disabled={!draft.trim() || isSending} aria-label="发送">↑</button></form>{skillMenuOpen && <div className="skill-menu">{skills.length === 0 ? <p>当前没有可用 Skill</p> : skills.map((skill) => <SkillHover name={skill.name} key={skill.name}><button className={selectedSkills.includes(skill.name) ? "chosen" : ""} onClick={() => { setSelectedSkills((old) => old.includes(skill.name) ? old.filter((item) => item !== skill.name) : [...old, skill.name]); setSkillMenuOpen(false); }}><span className="skill-menu-icon">✦</span><span><strong>{skill.name}</strong><small>{skill.description}</small></span><em>{selectedSkills.includes(skill.name) ? "已选" : skill.trust_level === "trusted" ? "可信" : "需授权"}</em></button></SkillHover>)}</div>}<div className="composer-meta"><span>Enter 发送 · Shift + Enter 换行</span>{activeTask?.run?.status === "running" && <button className="cancel" onClick={() => void cancel()}>停止生成</button>}</div></div></main>
    <aside className="workspace-panel"><div className="workspace-heading"><div><p className="eyebrow">SESSION WORKSPACE</p><h2>工作区</h2></div><button className="upload-button" disabled={!activeTask || isUploading} onClick={() => uploadInput.current?.click()}><Icon>↑</Icon> {isUploading ? "上传中" : "上传"}</button><input ref={uploadInput} className="hidden-input" type="file" onChange={(event) => void upload(event)} /></div>{!activeTask ? <div className="workspace-empty"><div>⌁</div><p>选择或创建一个会话后，即可使用独立工作区。</p></div> : <><p className="workspace-note">Agent 与 Skill 会把这里作为当前会话的文件读写目录，并按需读取文件。</p><div className="file-list">{files.length === 0 ? <p className="files-empty">还没有文件</p> : files.map((file) => <a className="file-row" key={file.path} href={`/api/tasks/${activeTask.task_id}/workspace/files/${file.path.split("/").map(encodeURIComponent).join("/")}`}><span className="file-icon">▱</span><span className="file-name">{file.path}<small>{formatSize(file.size_bytes)}</small></span><span className="download">↓</span></a>)}</div></>}</aside>
  </div>;
}
createRoot(document.getElementById("root")!).render(<App />);
