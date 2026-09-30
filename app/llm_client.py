import os
from typing import Union

from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage

# ----------------------------------------------------------------------
# Dummy model – returns a fixed response (configurable via env)
# ----------------------------------------------------------------------
class DummyChatModel:
    """A minimal stand‑in that mimics LangChain's async chat interface.

    It always returns a single message whose ``content`` is the configured
    ``LLM_DUMMY_RESPONSE`` (default ``"OK"``). This is useful for unit tests
    where we don't want to call an external LLM service.
    """

    def __init__(self, response: str = "OK"):
        self._response = response

    async def ainvoke(self, messages: list[BaseMessage]) -> dict:
        # Emulate LangChain's return format: a dict with a "messages" list
        class _Resp:
            def __init__(self, content: str):
                self.content = content

        return {"messages": [_Resp(self._response)]}


def get_chat_model() -> Union[ChatGroq, ChatOllama, ChatOpenAI, DummyChatModel]:
    """Factory that returns the appropriate LangChain chat model.

    Environment variables:
    - **LLM_PROVIDER**: ``groq``, ``ollama``, ``openai`` or ``dummy`` (default ``groq``).
    - **LLM_MODEL**: model name (e.g. ``openai/gpt-oss-120b`` for Groq).
    - **LLM_BASE_URL**: optional base URL for Ollama or OpenAI‑compatible servers.
    - **LLM_DUMMY_RESPONSE**: the fixed string the dummy model returns.
    """

    provider = os.getenv("LLM_PROVIDER", "groq").lower()
    model_name = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
    base_url = os.getenv("LLM_BASE_URL", "")

    if provider == "groq":
        return ChatGroq(model=model_name, temperature=0)
    if provider == "ollama":
        return ChatOllama(model=model_name, temperature=0, base_url=base_url or None)
    if provider == "openai":
        return ChatOpenAI(model=model_name, temperature=0, base_url=base_url or None)
    if provider == "dummy":
        dummy_resp = os.getenv("LLM_DUMMY_RESPONSE", "OK")
        return DummyChatModel(response=dummy_resp)

    raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")
