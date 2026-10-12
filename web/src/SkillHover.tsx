import { ReactNode, useEffect, useId, useRef, useState } from "react";

/** Read-only preview; hovering never selects or executes a Skill. */
export function SkillHover({ name, children, chip = false }: { name: string; children: ReactNode; chip?: boolean }) {
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState<string>();
  const [error, setError] = useState("");
  const [position, setPosition] = useState({ left: 12, top: 12 });
  const anchor = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout>>();
  const id = useId();
  const show = () => {
    clearTimeout(closeTimer.current);
    setOpen(true);
  };
  const hide = () => { closeTimer.current = setTimeout(() => setOpen(false), 180); };

  useEffect(() => () => clearTimeout(closeTimer.current), []);
  useEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = anchor.current?.getBoundingClientRect();
      if (!rect) return;
      const width = Math.min(520, window.innerWidth - 24);
      const height = Math.min(420, window.innerHeight - 24);
      const beside = rect.right + width + 20 <= window.innerWidth;
      setPosition({ left: Math.max(12, Math.min(beside ? rect.right + 8 : rect.left, window.innerWidth - width - 12)),
        top: Math.max(12, Math.min(beside ? rect.top : rect.top - height - 8, window.innerHeight - height - 12)) });
    };
    const dismiss = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    window.addEventListener("keydown", dismiss);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("keydown", dismiss);
    };
  }, [open]);

  useEffect(() => {
    if (!open || content !== undefined) return;
    const controller = new AbortController();
    setError("");
    void fetch(`/api/skills/${encodeURIComponent(name)}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("无法加载 Skill 完整内容，请移开后重试。");
        const detail = await response.json() as { content: string };
        if (!controller.signal.aborted) setContent(detail.content);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "无法加载 Skill 完整内容。");
      });
    return () => controller.abort();
  }, [open, name, content]);

  return <div ref={anchor} className={`skill-hover${chip ? " skill-hover-chip" : ""}`} tabIndex={chip ? 0 : undefined}
    onMouseEnter={show} onMouseLeave={hide} onFocus={show}
    onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) hide(); }}
    aria-controls={open ? id : undefined}>
    {children}
    {open && <section id={id} className="skill-preview" role="dialog" aria-label={`${name} 完整内容`}
      style={position} onMouseEnter={show} onMouseLeave={hide}>
      <header><strong>{name}</strong><span>SKILL.md · 完整内容</span></header>
      {content === undefined ? <p role="status">{error || "正在加载…"}</p> : <pre tabIndex={0}>{content}</pre>}
    </section>}
  </div>;
}
