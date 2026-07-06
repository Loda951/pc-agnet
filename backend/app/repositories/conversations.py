from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Conversation, MemoryFact, Message, ToolCall
from app.schemas.conversation import (
    ConversationDetail,
    ConversationMessageItem,
    ConversationSummary,
)


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ConversationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(self, user_id: int, conversation_id: int | None) -> Conversation:
        if conversation_id:
            existing = (
                await self.session.execute(
                    select(Conversation).where(
                        Conversation.id == conversation_id,
                        Conversation.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                return existing

        conversation = Conversation(user_id=user_id, title="PC 外设客服")
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def list_conversations(
        self, user_id: int, limit: int = 30
    ) -> list[ConversationSummary]:
        conversations = (
            (
                await self.session.execute(
                    select(Conversation)
                    .where(Conversation.user_id == user_id)
                    .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        summaries: list[ConversationSummary] = []
        for conversation in conversations:
            latest = (
                await self.session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            summaries.append(
                ConversationSummary(
                    id=conversation.id,
                    title=conversation.title,
                    created_at=conversation.created_at,
                    updated_at=conversation.updated_at,
                    last_message=_clip(latest.content) if latest else None,
                    last_message_role=latest.role if latest else None,
                    last_message_at=latest.created_at if latest else None,
                )
            )
        return summaries

    async def get_detail(self, user_id: int, conversation_id: int) -> ConversationDetail | None:
        conversation = (
            await self.session.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            return None

        messages = (
            (
                await self.session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.asc(), Message.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return ConversationDetail(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            messages=[
                ConversationMessageItem(
                    id=message.id,
                    role=message.role,
                    content=message.content,
                    metadata=message.metadata_json,
                    created_at=message.created_at,
                )
                for message in messages
                if message.role in {"user", "assistant"}
            ],
        )

    async def add_message(
        self, conversation_id: int, role: str, content: str, metadata: dict | None = None
    ) -> Message:
        conversation = await self.session.get(Conversation, conversation_id)
        if conversation:
            conversation.updated_at = utc_now_naive()
            if role == "user" and conversation.title == "PC 外设客服":
                conversation.title = _clip(content, 28)
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            metadata_json=metadata,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def list_recent_messages(self, conversation_id: int, limit: int = 12) -> list[Message]:
        messages = (
            (
                await self.session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.role.in_(["user", "assistant"]),
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(reversed(messages))

    async def get_working_memory(self, conversation_id: int) -> dict:
        conversation = await self.session.get(Conversation, conversation_id)
        if conversation is None or conversation.working_memory_json is None:
            return {}
        return dict(conversation.working_memory_json)

    async def update_working_memory(
        self, conversation_id: int, working_memory: dict
    ) -> None:
        conversation = await self.session.get(Conversation, conversation_id)
        if conversation:
            conversation.working_memory_json = working_memory
            conversation.updated_at = utc_now_naive()

    async def start_run(self, conversation_id: int, intent: str | None = None) -> AgentRun:
        run = AgentRun(conversation_id=conversation_id, status="running", intent=intent)
        self.session.add(run)
        await self.session.flush()
        return run

    async def finish_run(self, run: AgentRun, intent: str, state: dict) -> None:
        run.status = "completed"
        run.intent = intent
        run.state_json = state
        run.completed_at = utc_now_naive()

    async def fail_run(
        self, run_id: int, intent: str | None, state: dict, error: dict[str, str]
    ) -> None:
        run = await self.session.get(AgentRun, run_id)
        if not run:
            return
        run.status = "failed"
        run.intent = intent
        run.state_json = {**state, "error": error}
        run.completed_at = utc_now_naive()

    async def add_tool_call(
        self, agent_run_id: int, tool_name: str, input_json: dict, output_json: dict
    ) -> None:
        self.session.add(
            ToolCall(
                agent_run_id=agent_run_id,
                tool_name=tool_name,
                input_json=input_json,
                output_json=output_json,
            )
        )

    async def delete_conversation(self, user_id: int, conversation_id: int) -> bool:
        """Delete a conversation and all its messages, agent runs, and tool calls.

        Returns True if the conversation was found and deleted, False if not found.
        """
        conversation = (
            await self.session.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            return False

        # Delete tool calls for all agent runs in this conversation
        agent_runs = (
            await self.session.execute(
                select(AgentRun).where(AgentRun.conversation_id == conversation_id)
            )
        ).scalars().all()
        for run in agent_runs:
            await self.session.execute(
                ToolCall.__table__.delete().where(ToolCall.agent_run_id == run.id)
            )

        # Delete agent runs
        await self.session.execute(
            AgentRun.__table__.delete().where(AgentRun.conversation_id == conversation_id)
        )

        # Delete messages
        await self.session.execute(
            Message.__table__.delete().where(Message.conversation_id == conversation_id)
        )

        # Delete the conversation itself
        await self.session.delete(conversation)
        await self.session.flush()
        return True

    async def list_memory(self, user_id: int, limit: int = 10) -> list[MemoryFact]:
        now = utc_now_naive()
        stmt = (
            select(MemoryFact)
            .where(
                MemoryFact.user_id == user_id,
                MemoryFact.disabled_at.is_(None),
                or_(MemoryFact.expires_at.is_(None), MemoryFact.expires_at > now),
            )
            .order_by(MemoryFact.updated_at.desc())
            .limit(limit)
        )
        memories = list((await self.session.execute(stmt)).scalars().all())
        for memory in memories:
            memory.last_used_at = now
        return memories

    async def upsert_memory(
        self,
        user_id: int,
        key: str,
        value: str,
        confidence: float = 0.7,
        scope: str = "user",
        fact_type: str = "preference",
        source_message_id: int | None = None,
        expires_in_days: int | None = None,
    ) -> None:
        now = utc_now_naive()
        expires_at = now + timedelta(days=expires_in_days) if expires_in_days else None
        existing = (
            await self.session.execute(
                select(MemoryFact).where(
                    MemoryFact.user_id == user_id,
                    MemoryFact.scope == scope,
                    MemoryFact.fact_type == fact_type,
                    MemoryFact.key == key,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.value = value
            existing.confidence = confidence
            existing.source_message_id = source_message_id or existing.source_message_id
            existing.expires_at = expires_at
            existing.disabled_at = None
            existing.last_used_at = now
            existing.updated_at = now
        else:
            self.session.add(
                MemoryFact(
                    user_id=user_id,
                    scope=scope,
                    fact_type=fact_type,
                    key=key,
                    value=value,
                    confidence=confidence,
                    source_message_id=source_message_id,
                    expires_at=expires_at,
                    last_used_at=now,
                )
            )

    async def disable_memory(self, user_id: int, memory_id: int) -> bool:
        memory = (
            await self.session.execute(
                select(MemoryFact).where(
                    MemoryFact.id == memory_id,
                    MemoryFact.user_id == user_id,
                    MemoryFact.disabled_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if memory is None:
            return False
        now = utc_now_naive()
        memory.disabled_at = now
        memory.updated_at = now
        return True


def _clip(value: str, limit: int = 80) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"
