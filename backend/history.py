"""Ownership-scoped repository for anonymous conversation history."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import timezone, timedelta

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.exc import IntegrityError

from backend.config import HistorySettings
from backend.db import AnonymousCredential, Conversation, ConversationMessage, ConversationRun, Principal
from contracts.models import (ConversationMessagePage, ConversationMessageView, ConversationPage,
                              ConversationView, SessionEntry, ToolCall, new_id, utcnow)


class HistoryNotFound(Exception):
    """A resource is absent or not owned by the current principal."""


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _utc(value):
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _title(value: str) -> str:
    return " ".join(value.split())[:160] or "新会话"


def _conversation_view(row: Conversation) -> ConversationView:
    return ConversationView(id=row.id, title=row.title, created_at=row.created_at, updated_at=row.updated_at)


def _message_view(row: ConversationMessage) -> ConversationMessageView:
    return ConversationMessageView(id=row.id, sequence=row.sequence, role=row.role, content=row.content,
                                   tool_calls=[ToolCall.model_validate(item) for item in row.tool_calls_json],
                                   tool_call_id=row.tool_call_id, tool_name=row.tool_name, run_id=row.run_id,
                                   status=row.status, created_at=row.created_at)


class HistoryRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: HistorySettings) -> None:
        self.sessions = sessions
        self.settings = settings
        # V1 is deliberately single-instance SQLite. This serializes local sequence allocation;
        # SQLite WAL/busy_timeout handles short database-level contention as well.
        self._write_lock = asyncio.Lock()

    async def resolve_anonymous(self, raw_token: str | None) -> tuple[str, str | None]:
        """Return (principal_id, cookie_to_set); raw tokens never enter the database."""
        now = utcnow()
        if raw_token:
            digest = token_hash(raw_token)
            async with self.sessions.begin() as session:
                credential = await session.get(AnonymousCredential, digest)
                if credential and credential.revoked_at is None and _utc(credential.expires_at) > now:
                    credential.last_seen_at = now
                    return credential.principal_id, None
        raw_token = secrets.token_urlsafe(32)  # 256 bits of entropy before URL encoding.
        async with self.sessions.begin() as session:
            principal_id = new_id("principal")
            session.add(Principal(id=principal_id, kind="anonymous", created_at=now))
            # These models deliberately avoid ORM relationship loading; flush the parent first
            # so SQLite's immediate foreign-key enforcement sees it.
            await session.flush()
            session.add(AnonymousCredential(token_hash=token_hash(raw_token), principal_id=principal_id,
                                            expires_at=now + timedelta(days=self.settings.cookie_max_age_days),
                                            last_seen_at=now))
        return principal_id, raw_token

    async def revoke_credential(self, raw_token: str) -> None:
        async with self.sessions.begin() as session:
            credential = await session.get(AnonymousCredential, token_hash(raw_token))
            if credential:
                credential.revoked_at = utcnow()

    async def create_conversation(self, principal_id: str, title: str = "新会话") -> ConversationView:
        now = utcnow()
        conversation = Conversation(id=new_id("conv"), owner_principal_id=principal_id, title=_title(title),
                                    created_at=now, updated_at=now)
        async with self.sessions.begin() as session:
            session.add(conversation)
        return _conversation_view(conversation)

    async def _owned_conversation(self, session: AsyncSession, principal_id: str, conversation_id: str) -> Conversation:
        row = await session.scalar(select(Conversation).where(Conversation.id == conversation_id,
                                   Conversation.owner_principal_id == principal_id, Conversation.deleted_at.is_(None)))
        if row is None:
            raise HistoryNotFound
        return row

    async def list_conversations(self, principal_id: str, cursor: str | None, limit: int = 30) -> ConversationPage:
        limit = max(1, min(limit, 100))
        async with self.sessions() as session:
            statement: Select = select(Conversation).where(Conversation.owner_principal_id == principal_id,
                            Conversation.deleted_at.is_(None)).order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit + 1)
            if cursor:
                before = await session.get(Conversation, cursor)
                if before is None or before.owner_principal_id != principal_id or before.deleted_at is not None:
                    raise HistoryNotFound
                statement = statement.where((Conversation.updated_at < before.updated_at) |
                                            ((Conversation.updated_at == before.updated_at) & (Conversation.id < before.id)))
            rows = list((await session.scalars(statement)).all())
        page_rows, overflow = rows[:limit], rows[limit:]
        return ConversationPage(items=[_conversation_view(row) for row in page_rows],
                                next_cursor=page_rows[-1].id if overflow and page_rows else None)

    async def get_conversation(self, principal_id: str, conversation_id: str) -> ConversationView:
        async with self.sessions() as session:
            return _conversation_view(await self._owned_conversation(session, principal_id, conversation_id))

    async def update_title(self, principal_id: str, conversation_id: str, title: str) -> ConversationView:
        async with self.sessions.begin() as session:
            row = await self._owned_conversation(session, principal_id, conversation_id)
            row.title, row.updated_at = _title(title), utcnow()
            return _conversation_view(row)

    async def delete_conversation(self, principal_id: str, conversation_id: str) -> None:
        async with self.sessions.begin() as session:
            row = await self._owned_conversation(session, principal_id, conversation_id)
            row.deleted_at = row.updated_at = utcnow()

    async def list_messages(self, principal_id: str, conversation_id: str, cursor: int | None,
                            limit: int = 100) -> ConversationMessagePage:
        limit = max(1, min(limit, 200))
        async with self.sessions() as session:
            await self._owned_conversation(session, principal_id, conversation_id)
            statement: Select = select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id)
            if cursor is not None:
                statement = statement.where(ConversationMessage.sequence > cursor)
            rows = list((await session.scalars(statement.order_by(ConversationMessage.sequence).limit(limit + 1))).all())
        page_rows, overflow = rows[:limit], rows[limit:]
        return ConversationMessagePage(items=[_message_view(row) for row in page_rows],
                                       next_cursor=str(page_rows[-1].sequence) if overflow and page_rows else None)

    async def create_run_mapping(self, *, principal_id: str, conversation_id: str, task_id: str, run_id: str,
                                 session_id: str, project_id: str, user_input: str, request_key: str | None = None) -> None:
        now = utcnow()
        async with self._write_lock:
            async with self.sessions.begin() as session:
                conversation = await self._owned_conversation(session, principal_id, conversation_id)
                session.add(ConversationRun(conversation_id=conversation_id, run_id=run_id, task_id=task_id,
                                            session_id=session_id, project_id=project_id, input=user_input,
                                            request_key=request_key, created_at=now))
                await self._append_message(session, conversation, role="user", content=user_input, run_id=run_id,
                                           status="completed", source_entry_id=f"user:{run_id}", created_at=now)
                if conversation.title == "新会话":
                    conversation.title = _title(user_input)
                conversation.updated_at = now

    async def _append_message(self, session: AsyncSession, conversation: Conversation, *, role: str, content: str,
                              run_id: str | None, status: str, source_entry_id: str | None,
                              tool_calls: list[dict] | None = None, tool_call_id: str | None = None,
                              tool_name: str | None = None, created_at=None) -> bool:
        if source_entry_id:
            exists = await session.scalar(select(ConversationMessage.id).where(
                ConversationMessage.conversation_id == conversation.id,
                ConversationMessage.source_entry_id == source_entry_id))
            if exists:
                return False
        sequence = (await session.scalar(select(func.coalesce(func.max(ConversationMessage.sequence), 0)).where(
            ConversationMessage.conversation_id == conversation.id))) + 1
        session.add(ConversationMessage(id=new_id("msg"), conversation_id=conversation.id, sequence=sequence,
                                        role=role, content=content, tool_calls_json=tool_calls or [],
                                        tool_call_id=tool_call_id, tool_name=tool_name, run_id=run_id, status=status,
                                        source_entry_id=source_entry_id, created_at=created_at or utcnow()))
        return True

    async def mapping_for_task(self, principal_id: str, task_id: str) -> ConversationRun:
        async with self.sessions() as session:
            row = await session.scalar(select(ConversationRun).join(Conversation).where(
                ConversationRun.task_id == task_id, Conversation.owner_principal_id == principal_id,
                Conversation.deleted_at.is_(None)))
            if row is None:
                raise HistoryNotFound
            return row

    async def mapping_for_request_key(self, principal_id: str, request_key: str) -> ConversationRun | None:
        async with self.sessions() as session:
            return await session.scalar(select(ConversationRun).join(Conversation).where(
                ConversationRun.request_key == request_key, Conversation.owner_principal_id == principal_id,
                Conversation.deleted_at.is_(None)))

    async def list_mappings(self, principal_id: str, limit: int = 100) -> list[ConversationRun]:
        async with self.sessions() as session:
            return list((await session.scalars(select(ConversationRun).join(Conversation).where(
                Conversation.owner_principal_id == principal_id, Conversation.deleted_at.is_(None)).order_by(
                ConversationRun.created_at.desc()).limit(max(1, min(limit, 100))))).all())

    async def mapping_for_run(self, principal_id: str, run_id: str) -> ConversationRun:
        async with self.sessions() as session:
            row = await session.scalar(select(ConversationRun).join(Conversation).where(
                ConversationRun.run_id == run_id, Conversation.owner_principal_id == principal_id,
                Conversation.deleted_at.is_(None)))
            if row is None:
                raise HistoryNotFound
            return row

    async def latest_mapping(self, principal_id: str, conversation_id: str) -> ConversationRun | None:
        async with self.sessions() as session:
            await self._owned_conversation(session, principal_id, conversation_id)
            return await session.scalar(select(ConversationRun).where(
                ConversationRun.conversation_id == conversation_id).order_by(ConversationRun.created_at.desc()))

    async def append_harness_entries(self, principal_id: str, run_id: str, entries: list[SessionEntry]) -> None:
        """Idempotently materialize finished structured Harness entries after SSE events."""
        async with self._write_lock:
            async with self.sessions.begin() as session:
                mapping = await session.scalar(select(ConversationRun).join(Conversation).where(
                    ConversationRun.run_id == run_id, Conversation.owner_principal_id == principal_id,
                    Conversation.deleted_at.is_(None)))
                if mapping is None:
                    raise HistoryNotFound
                conversation = await self._owned_conversation(session, principal_id, mapping.conversation_id)
                added = False
                for entry in entries:
                    if entry.role == "system" or (entry.role == "user" and entry.content == mapping.input):
                        continue
                    added |= await self._append_message(
                        session, conversation, role=entry.role, content=entry.content, run_id=run_id,
                        status="completed", source_entry_id=entry.id,
                        tool_calls=[call.model_dump(mode="json") for call in entry.tool_calls],
                        tool_call_id=entry.tool_call_id, tool_name=entry.tool_name, created_at=entry.created_at)
                if added:
                    conversation.updated_at = utcnow()
