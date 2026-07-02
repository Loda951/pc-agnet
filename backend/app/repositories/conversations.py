from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Conversation, MemoryFact, Message, ToolCall


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

    async def add_message(
        self, conversation_id: int, role: str, content: str, metadata: dict | None = None
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            metadata_json=metadata,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def start_run(self, conversation_id: int, intent: str | None = None) -> AgentRun:
        run = AgentRun(conversation_id=conversation_id, status="running", intent=intent)
        self.session.add(run)
        await self.session.flush()
        return run

    async def finish_run(self, run: AgentRun, intent: str, state: dict) -> None:
        run.status = "completed"
        run.intent = intent
        run.state_json = state
        run.completed_at = datetime.now(UTC)

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

    async def list_memory(self, user_id: int, limit: int = 10) -> list[MemoryFact]:
        stmt = (
            select(MemoryFact)
            .where(MemoryFact.user_id == user_id)
            .order_by(MemoryFact.updated_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def upsert_memory(
        self, user_id: int, key: str, value: str, confidence: float = 0.7
    ) -> None:
        existing = (
            await self.session.execute(
                select(MemoryFact).where(MemoryFact.user_id == user_id, MemoryFact.key == key)
            )
        ).scalar_one_or_none()
        if existing:
            existing.value = value
            existing.confidence = confidence
            existing.updated_at = datetime.now(UTC)
        else:
            self.session.add(
                MemoryFact(user_id=user_id, key=key, value=value, confidence=confidence)
            )
