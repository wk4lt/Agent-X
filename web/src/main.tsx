import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Run = { run_id: string; status: string; stop_reason?: string; final_answer?: string };
type Task = { task_id: string; input: string; project_id: string; conversation_id?: string; run?: Run };
type Conversation = { id: string; title: string; created_at: string; updated_at: string };
type Message = { id: string; sequence: number; role: "user" | "assistant" | "tool"; content: string; status: string };
type Skill = { name: string; description: string; source: string; trust_level: string };
type WorkspaceFile = { name: string; size_bytes: number; modified_at: string };
type Envelope = { sequence: number; type: string; payload: Record<string, unknown> };
type TokenStatus = { estimated?: string; actual?: string; compaction?: string };

const terminalEvents = new Set(["run.completed", "run.failed", "run.cancelled", "run.interrupted"]);
const eventNames = ["run.accepted", "run.started", "context.built", "context.compaction.started", "context.compaction.completed", "context.compaction.failed", "skill.discovered", "skill.loaded", "skill.load_denied", "skill.invalid", "skill.script.approval_required", "skill.script.started", "skill.script.stdout", "skill.script.completed", "skill.script.failed", "assistant.delta", "assistant.message", "tool.started", "tool.completed", "tool.failed", "usage.updated", ...terminalEvents];

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
    if (event.type === "assistant.delta") setAnswer((old) => old + String(event.payload.delta ?? ""));
    if (event.type === "assistant.message") setAnswer(String(event.payload.content ?? ""));
    if (event.type.startsWith("tool.")) setActivity(event.type === "tool.started" ? "正在使用工具…" : "工具调用已完成");
    if (event.type === "skill.loaded") setActivity(`已加载 Skill：${String(event.payload.name ?? "")}`);
    if (event.type === "skill.script.approval_required") setActivity("Skill 脚本等待授权");
    if (event.type === "skill.script.started") setActivity("正在运行 Skill 脚本…");
    if (event.type === "skill.script.completed") setActivity("Skill 脚本已完成");
    if (event.type === "skill.script.failed") setActivity("Skill 脚本未完成");
    if (event.type === "run.started") setActivity("正在思考…");
    if (event.type === "context.built") {
      if (event.payload.status === "ready") {
        setTokens((old) => ({ ...old, estimated: `${event.payload.input_tokens_after_est ?? 0} / ${event.payload.available_input_tokens ?? 0} 估算输入 Token`, compaction: undefined }));
      } else setActivity("上下文预算不足");
    }
    if (event.type === "context.compaction.started") { setActivity("正在压缩较早的历史…"); setTokens((old) => ({ ...old, compaction: "历史压缩中" })); }
    if (event.type === "context.compaction.completed") { setTokens((old) => ({ ...old, compaction: "历史已压缩" })); }
    if (event.type === "context.compaction.failed") { setTokens((old) => ({ ...old, compaction: "历史压缩失败，已降级裁剪" })); }
    if (event.type === "usage.updated" && event.payload.kind === "run") {
      const input = event.payload.prompt_tokens_actual ?? "—", output = event.payload.completion_tokens_actual ?? "—";
      setTokens((old) => ({ ...old, actual: `实际：输入 ${input} · 输出 ${output}` }));
    }
    if (terminalEvents.has(event.type)) {
      stream.current?.close();
      const status = event.type.slice(4), stopReason = String(event.payload.stop_reason ?? "");
      setActivity(status === "completed" ? "已完成" : `已停止：${stopReason || status}`);
      setActiveTask((old) => old?.task_id === taskId && old.run ? { ...old, run: { ...old.run, status, stop_reason: stopReason } } : old);
      setTasks((old) => old.map((task) => task.task_id === taskId && task.run ? { ...task, run: { ...task.run, status, stop_reason: stopReason } } : task));
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
    stream.current?.close(); setActiveTask(task); setAnswer(""); setActivity(""); setError(""); setFiles([]); setTokens({});
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
    stream.current?.close(); setActiveConversation(conversation); setActiveTask(undefined); setAnswer(""); setActivity(""); setError(""); setFiles([]); setTokens({});
    await loadConversationMessages(conversation.id);
    const response = await fetch(`/api/conversations/${conversation.id}/latest-task`);
    if (response.ok) { const task = await response.json() as Task | null; if (task) { setActiveTask(task); await loadFiles(task.task_id); if (task.run && !["completed", "failed", "cancelled", "interrupted"].includes(task.run.status)) connect(task.task_id); } }
  };
  const newSession = async () => {
    stream.current?.close(); setActiveTask(undefined); setDraft(""); setAnswer(""); setActivity(""); setError(""); setFiles([]); setTokens({}); setMessages([]);
    const response = await fetch("/api/conversations", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({}) });
    if (!response.ok) { setError("无法新建会话。"); return; }
    const conversation = await response.json() as Conversation; setActiveConversation(conversation); await loadConversations();
  };
  async function submit(event: FormEvent) {
    event.preventDefault(); const input = draft.trim(); if (!input || isSending) return;
    setIsSending(true); setError(""); setAnswer(""); setActivity("正在创建会话…"); setTokens({});
    try {
      let conversation = activeConversation;
      if (!conversation) { const createdConversation = await fetch("/api/conversations", { method: "POST" }); if (!createdConversation.ok) throw new Error("创建会话失败"); conversation = await createdConversation.json() as Conversation; setActiveConversation(conversation); }
      const response = await fetch("/api/tasks", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ input, conversation_id: conversation.id, selected_skills: selectedSkills }) });
      if (!response.ok) throw new Error(`创建失败 (${response.status})`);
      const created = await response.json() as Task;
      setTasks((old) => [created, ...old.filter((task) => task.task_id !== created.task_id)]);
      setActiveTask(created); setMessages((old) => [...old, { id: `optimistic-${created.task_id}`, sequence: -1, role: "user", content: input, status: "completed" }]); setDraft(""); setSelectedSkills([]); setFiles([]); connect(created.task_id); await loadConversations();
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
    <aside className="sidebar"><div className="brand"><span className="brand-mark">✦</span><span>TroubleShooter</span></div><button className="new-chat" onClick={() => void newSession()}><Icon>＋</Icon> 新建会话</button>
      <nav className="session-list" aria-label="会话列表"><p className="section-label">历史会话</p>{conversations.length === 0 && <p className="empty-list">尚未创建会话</p>}{conversations.map((conversation) => <div className={`session-row ${activeConversation?.id === conversation.id ? "selected" : ""}`} key={conversation.id}><button className="session-item" onClick={() => void selectConversation(conversation)}><Icon>◌</Icon><span>{conversation.title}</span></button><button className="delete-chat" aria-label="删除会话" onClick={async () => { if (!window.confirm("删除此会话？")) return; await fetch(`/api/conversations/${conversation.id}`, { method: "DELETE" }); if (activeConversation?.id === conversation.id) { setActiveConversation(undefined); setActiveTask(undefined); setMessages([]); } await loadConversations(); }}>×</button></div>)}</nav><div className="sidebar-footer"><span className="avatar">S</span><span>本地开发环境</span></div></aside>
    <main className="chat-area"><header className="chat-header"><div><strong>{activeConversation?.title || "新会话"}</strong><span className="model-chip">DeepSeek</span>{(tokens.estimated || tokens.actual || tokens.compaction) && <span className="token-status">{tokens.estimated}{tokens.actual && <> · {tokens.actual}</>}{tokens.compaction && <> · {tokens.compaction}</>}</span>}</div>{activeTask?.run && <span className={`status status-${activeTask.run.status}`}>{activeTask.run.status}</span>}</header>
      <div className="conversation">{!activeConversation && <div className="welcome"><div className="welcome-mark">✦</div><h1>今天想完成什么？</h1><p>历史会话与工作区会保存在当前浏览器的匿名身份下。</p></div>}{activeConversation && <>{messages.filter((message) => message.role !== "tool").map((message) => <article key={message.id} className={`message ${message.role === "user" ? "user-message" : "assistant-message"}`}><div className={`message-avatar ${message.role === "assistant" ? "assistant-avatar" : ""}`}>{message.role === "user" ? "你" : "✦"}</div><div className="answer">{message.content}</div></article>)}{activeTask && answer && <article className="message assistant-message"><div className="message-avatar assistant-avatar">✦</div><div className="answer">{answer}</div></article>}{activeTask && !answer && activeTask.run && <article className="message assistant-message"><div className="message-avatar assistant-avatar">✦</div><div className="answer"><span className="typing">{activity || "正在准备回答…"}</span></div></article>}</>}{error && <p className="notice error-notice">{error}</p>}</div>
      <div className="composer-wrap"><div className="skill-tray">{selectedSkills.map((name) => <span className="skill-chip" key={name}>✦ {name}<button aria-label={`移除 ${name}`} onClick={() => setSelectedSkills((old) => old.filter((item) => item !== name))}>×</button></span>)}</div><form className="composer" onSubmit={submit}><button className="skill-picker" type="button" title="选择 Skill" onClick={() => setSkillMenuOpen((open) => !open)}>✦ <span>Skills</span></button><textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="给 TroubleShooter 发送消息" rows={1} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} /><button className="send" type="submit" disabled={!draft.trim() || isSending} aria-label="发送">↑</button></form>{skillMenuOpen && <div className="skill-menu">{skills.length === 0 ? <p>当前没有可用 Skill</p> : skills.map((skill) => <button key={skill.name} className={selectedSkills.includes(skill.name) ? "chosen" : ""} onClick={() => setSelectedSkills((old) => old.includes(skill.name) ? old.filter((item) => item !== skill.name) : [...old, skill.name])}><span className="skill-menu-icon">✦</span><span><strong>{skill.name}</strong><small>{skill.description}</small></span><em>{selectedSkills.includes(skill.name) ? "已选" : skill.trust_level === "trusted" ? "可信" : "需授权"}</em></button>)}</div>}<div className="composer-meta"><span>Enter 发送 · Shift + Enter 换行</span>{activeTask?.run?.status === "running" && <button className="cancel" onClick={() => void cancel()}>停止生成</button>}</div></div></main>
    <aside className="workspace-panel"><div className="workspace-heading"><div><p className="eyebrow">SESSION WORKSPACE</p><h2>工作区</h2></div><button className="upload-button" disabled={!activeTask || isUploading} onClick={() => uploadInput.current?.click()}><Icon>↑</Icon> {isUploading ? "上传中" : "上传"}</button><input ref={uploadInput} className="hidden-input" type="file" onChange={(event) => void upload(event)} /></div>{!activeTask ? <div className="workspace-empty"><div>⌁</div><p>选择或创建一个会话后，即可使用独立工作区。</p></div> : <><p className="workspace-note">文件仅属于当前会话，暂不会自动发送给模型。</p><div className="file-list">{files.length === 0 ? <p className="files-empty">还没有文件</p> : files.map((file) => <a className="file-row" key={file.name} href={`/api/tasks/${activeTask.task_id}/workspace/files/${encodeURIComponent(file.name)}`}><span className="file-icon">▱</span><span className="file-name">{file.name}<small>{formatSize(file.size_bytes)}</small></span><span className="download">↓</span></a>)}</div></>}</aside>
  </div>;
}
createRoot(document.getElementById("root")!).render(<App />);
