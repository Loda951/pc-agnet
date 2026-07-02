from langchain_openai import ChatOpenAI

from app.core.config import Settings


def resolve_llm_base_url(settings: Settings) -> str:
    if settings.llm_base_url:
        return settings.llm_base_url
    if settings.llm_provider == "deepseek":
        return "https://api.deepseek.com"
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


def build_chat_model(settings: Settings) -> ChatOpenAI | None:
    if not settings.llm_api_key:
        return None

    return ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=resolve_llm_base_url(settings),
        model=settings.llm_model,
        temperature=0.2,
    )
