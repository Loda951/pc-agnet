import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    return await AgentRuntime(session, settings).run(request)


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        response = await AgentRuntime(session, settings).run(request)
        payload = response.model_dump(mode="json")
        for line in response.answer.splitlines() or [response.answer]:
            yield f"data: {json.dumps({'type': 'delta', 'content': line}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'response': payload}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
