from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from backend.client import HarnessClient
from backend.config import HistorySettings, bearer_api_key
from backend.db import create_database
from backend.history import HistoryNotFound, HistoryRepository
from backend.migrate import run_migrations
from contracts.models import (ConversationMessagePage, ConversationPage, ConversationView, CreateRunRequest,
                              CreateTaskRequest, RunStatus, TaskView, WorkspaceFile, new_id)
from contracts.session_storage import append_session_log, session_directory


def create_app(client: HarnessClient | None = None, workspace_root: Path | None = None,
               log_root: Path | None = None, history_settings: HistorySettings | None = None) -> FastAPI:
    settings = history_settings or HistorySettings.from_environment()
    engine, sessions = create_database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A versioned Alembic upgrade, not create_all(), initializes and upgrades storage.
        run_migrations(settings)
        try:
            settings.database_path.chmod(0o600)
        except OSError:
            pass
        try:
            yield
        finally:
            for task in app.state.persistence_tasks:
                task.cancel()
            await engine.dispose()

    app = FastAPI(title="Agent-X Backend", version="0.1.0", lifespan=lifespan)
    app.state.client = client or HarnessClient()
    app.state.history = HistoryRepository(sessions, settings)
    app.state.persistence_tasks: set[asyncio.Task] = set()
    root = (workspace_root or Path(os.getenv("WORKSPACE_ROOT", ".data/workspaces"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app.state.workspace_root = root
    app.state.workspace_max_file_bytes = int(os.getenv("WORKSPACE_MAX_FILE_BYTES", str(25 * 1024 * 1024)))
    logs = (log_root or Path(os.getenv("LOG_ROOT", ".data/logs"))).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    app.state.log_root = logs

    @app.middleware("http")
    async def caller_identity(request: Request, call_next):
        try:
            api_key = bearer_api_key(request.headers.get("authorization"), settings.api_keys)
        except ValueError as exc:
            return Response(status_code=401, content=str(exc), headers={"WWW-Authenticate": "Bearer"})
        if api_key is not None:
            request.state.principal_id = await app.state.history.resolve_api_key(api_key)
            return await call_next(request)
        principal_id, issued_token = await app.state.history.resolve_anonymous(
            request.cookies.get(settings.cookie_name)
        )
        request.state.principal_id = principal_id
        response = await call_next(request)
        if issued_token:
            response.set_cookie(settings.cookie_name, issued_token, max_age=settings.cookie_max_age_days * 86_400,
                                secure=settings.cookie_secure, httponly=settings.cookie_http_only,
                                samesite=settings.cookie_same_site, path="/")
        return response

    def principal(request: Request) -> str:
        return request.state.principal_id

    def missing() -> HTTPException:
        return HTTPException(404, "resource_not_found")

    async def task_mapping(request: Request, task_id: str):
        try:
            return await app.state.history.mapping_for_task(principal(request), task_id)
        except HistoryNotFound as exc:
            raise missing() from exc

    def workspace(mapping) -> Path:
        try:
            # A conversation owns a stable workspace across its many Task/Run/Session executions.
            return session_directory(root, mapping.conversation_id)
        except ValueError as exc:
            raise HTTPException(400, "invalid_workspace") from exc

    def audit(mapping, event: str, **fields: object) -> None:
        try:
            append_session_log(logs, mapping.session_id, "backend", event, fields)
        except OSError:
            pass

    def workspace_file(mapping, filename: str) -> Path:
        name = Path(filename).name
        if not name or name in {".", ".."}:
            raise HTTPException(422, "invalid_filename")
        directory = workspace(mapping)
        destination = (directory / name).resolve()
        if destination.parent != directory:
            raise HTTPException(422, "invalid_filename")
        return destination

    def file_view(path: Path) -> WorkspaceFile:
        from datetime import datetime, timezone
        details = path.stat()
        return WorkspaceFile(name=path.name, size_bytes=details.st_size,
                             modified_at=datetime.fromtimestamp(details.st_mtime, timezone.utc))

    def task_view(mapping, run=None) -> TaskView:
        return TaskView(task_id=mapping.task_id, project_id=mapping.project_id, input=mapping.input,
                        conversation_id=mapping.conversation_id, run=run)

    async def persist_run_entries(principal_id: str, run_id: str) -> None:
        entries = await app.state.client.session_entries(run_id)
        await app.state.history.append_harness_entries(principal_id, run_id, entries)

    def schedule_terminal_persistence(principal_id: str, run_id: str) -> None:
        async def watch() -> None:
            try:
                while True:
                    run = await app.state.client.get_run(run_id)
                    if run.status in {RunStatus.completed, RunStatus.failed, RunStatus.cancelled, RunStatus.interrupted}:
                        await persist_run_entries(principal_id, run_id)
                        return
                    await asyncio.sleep(0.2)
            except (httpx.HTTPError, HistoryNotFound):
                # SSE will surface Harness failures. Do not leak request/session content in logs.
                return
        watcher = asyncio.create_task(watch())
        app.state.persistence_tasks.add(watcher)
        watcher.add_done_callback(app.state.persistence_tasks.discard)

    @app.get("/api/conversations", response_model=ConversationPage)
    async def list_conversations(request: Request, cursor: str | None = None, limit: int = 30) -> ConversationPage:
        try:
            return await app.state.history.list_conversations(principal(request), cursor, limit)
        except HistoryNotFound as exc:
            raise missing() from exc

    @app.post("/api/conversations", response_model=ConversationView, status_code=201)
    async def create_conversation(request: Request, payload: dict | None = None) -> ConversationView:
        title = (payload or {}).get("title", "新会话")
        if not isinstance(title, str):
            raise HTTPException(422, "invalid_title")
        return await app.state.history.create_conversation(principal(request), title)

    @app.get("/api/conversations/{conversation_id}/messages", response_model=ConversationMessagePage)
    async def list_messages(request: Request, conversation_id: str, cursor: int | None = None,
                            limit: int = 100) -> ConversationMessagePage:
        try:
            return await app.state.history.list_messages(principal(request), conversation_id, cursor, limit)
        except HistoryNotFound as exc:
            raise missing() from exc

    @app.patch("/api/conversations/{conversation_id}", response_model=ConversationView)
    async def update_conversation(request: Request, conversation_id: str, payload: dict) -> ConversationView:
        title = payload.get("title")
        if not isinstance(title, str):
            raise HTTPException(422, "invalid_title")
        try:
            return await app.state.history.update_title(principal(request), conversation_id, title)
        except HistoryNotFound as exc:
            raise missing() from exc

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(request: Request, conversation_id: str) -> Response:
        try:
            await app.state.history.delete_conversation(principal(request), conversation_id)
        except HistoryNotFound as exc:
            raise missing() from exc
        return Response(status_code=204)

    @app.post("/api/tasks", status_code=202)
    async def create_task(request: Request, payload: CreateTaskRequest,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> TaskView:
        if idempotency_key:
            if len(idempotency_key) > 200:
                raise HTTPException(422, "invalid_idempotency_key")
            existing = await app.state.history.mapping_for_request_key(principal(request), idempotency_key)
            if existing:
                try:
                    return task_view(existing, await app.state.client.get_run(existing.run_id))
                except httpx.HTTPError as exc:
                    raise HTTPException(502, "harness_unavailable") from exc
        conversation_id = payload.conversation_id
        if conversation_id is None:
            conversation = await app.state.history.create_conversation(principal(request), payload.input)
            conversation_id = conversation.id
        else:
            try:
                await app.state.history.get_conversation(principal(request), conversation_id)
            except HistoryNotFound as exc:
                raise missing() from exc
        task_id = new_id("task")
        try:
            run = await app.state.client.create_run(CreateRunRequest(task_id=task_id,
                idempotency_key=f"task-create:{task_id}", input=payload.input, selected_skills=payload.selected_skills,
                workspace_id=conversation_id))
            await app.state.history.create_run_mapping(principal_id=principal(request), conversation_id=conversation_id,
                task_id=task_id, run_id=run.run_id, session_id=run.session_id, project_id=payload.project_id,
                user_input=payload.input, request_key=idempotency_key)
        except httpx.HTTPError as exc:
            raise HTTPException(502, "harness_unavailable") from exc
        except HistoryNotFound as exc:
            raise missing() from exc
        mapping = await task_mapping(request, task_id)
        schedule_terminal_persistence(principal(request), run.run_id)
        audit(mapping, "task.created", task_id=task_id, run_id=run.run_id)
        return task_view(mapping, run)

    @app.get("/api/skills")
    async def list_skills() -> list[dict]:
        try:
            return await app.state.client.skills()
        except httpx.HTTPError as exc:
            raise HTTPException(502, "harness_unavailable") from exc

    @app.get("/api/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> TaskView:
        mapping = await task_mapping(request, task_id)
        try:
            run = await app.state.client.get_run(mapping.run_id)
        except httpx.HTTPError as exc:
            raise HTTPException(502, "harness_unavailable") from exc
        return task_view(mapping, run)

    @app.get("/api/tasks")
    async def list_tasks(request: Request) -> list[TaskView]:
        return [task_view(mapping) for mapping in await app.state.history.list_mappings(principal(request))]

    @app.get("/api/conversations/{conversation_id}/latest-task")
    async def latest_conversation_task(request: Request, conversation_id: str) -> TaskView | None:
        try:
            mapping = await app.state.history.latest_mapping(principal(request), conversation_id)
        except HistoryNotFound as exc:
            raise missing() from exc
        if mapping is None:
            return None
        try:
            run = await app.state.client.get_run(mapping.run_id)
        except httpx.HTTPError:
            run = None
        return task_view(mapping, run)

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(request: Request, task_id: str) -> TaskView:
        mapping = await task_mapping(request, task_id)
        try:
            run = await app.state.client.cancel(mapping.run_id)
        except httpx.HTTPError as exc:
            raise HTTPException(502, "harness_unavailable") from exc
        return task_view(mapping, run)

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(request: Request, task_id: str) -> TaskView:
        mapping = await task_mapping(request, task_id)
        try:
            run = await app.state.client.resume(mapping.run_id)
            await app.state.history.create_run_mapping(principal_id=principal(request), conversation_id=mapping.conversation_id,
                task_id=new_id("task"), run_id=run.run_id, session_id=run.session_id, project_id=mapping.project_id,
                user_input=mapping.input)
        except httpx.HTTPError as exc:
            raise HTTPException(502, "harness_unavailable") from exc
        replacement = await app.state.history.mapping_for_run(principal(request), run.run_id)
        schedule_terminal_persistence(principal(request), run.run_id)
        audit(replacement, "task.resumed", task_id=replacement.task_id, run_id=run.run_id)
        return task_view(replacement, run)

    @app.get("/api/tasks/{task_id}/workspace/files")
    async def list_workspace_files(request: Request, task_id: str) -> list[WorkspaceFile]:
        directory = workspace(await task_mapping(request, task_id))
        return [file_view(path) for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()) if path.is_file()]

    @app.post("/api/tasks/{task_id}/workspace/files", status_code=201)
    async def upload_workspace_file(request: Request, task_id: str, file: UploadFile = File(...)) -> WorkspaceFile:
        mapping = await task_mapping(request, task_id)
        destination = workspace_file(mapping, file.filename or "")
        if destination.exists():
            raise HTTPException(409, "workspace_file_exists")
        total = 0
        try:
            with destination.open("xb") as output:
                while chunk := await file.read(64 * 1024):
                    total += len(chunk)
                    if total > app.state.workspace_max_file_bytes:
                        raise HTTPException(413, "workspace_file_too_large")
                    output.write(chunk)
        except HTTPException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        uploaded = file_view(destination)
        audit(mapping, "workspace.file_uploaded", name=uploaded.name, size_bytes=uploaded.size_bytes)
        return uploaded

    @app.get("/api/tasks/{task_id}/workspace/files/{filename}")
    async def download_workspace_file(request: Request, task_id: str, filename: str) -> FileResponse:
        mapping = await task_mapping(request, task_id)
        path = workspace_file(mapping, filename)
        if not path.is_file():
            raise HTTPException(404, "workspace_file_not_found")
        audit(mapping, "workspace.file_downloaded", name=path.name, size_bytes=path.stat().st_size)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(request: Request, task_id: str, after_sequence: int = 0) -> StreamingResponse:
        mapping = await task_mapping(request, task_id)
        last_id = request.headers.get("last-event-id")
        cursor = max(after_sequence, int(last_id)) if last_id and last_id.isdigit() else after_sequence
        owner = principal(request)

        async def stream() -> AsyncIterator[bytes]:
            pending = ""
            try:
                async for chunk in app.state.client.events(mapping.run_id, cursor):
                    pending += chunk.decode("utf-8")
                    while "\n\n" in pending:
                        block, pending = pending.split("\n\n", 1)
                        if "event: assistant.message" in block or "event: run.completed" in block:
                            await persist_run_entries(owner, mapping.run_id)
                        yield (block + "\n\n").encode("utf-8")
                if pending:
                    yield pending.encode("utf-8")
            except (httpx.HTTPError, HistoryNotFound):
                yield b"event: backend.error\ndata: {\"code\":\"harness_stream_failed\"}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = create_app()
