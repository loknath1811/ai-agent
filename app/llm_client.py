"""The ONE place that decides which AI model is used.

Switch models by editing .env only - no code changes:

  LLM_PROVIDER=groq      (cloud; prototype only)   needs GROQ_API_KEY
  LLM_PROVIDER=ollama    (in-house, local server)  LLM_BASE_URL=http://server:11434
  LLM_PROVIDER=openai    (any OpenAI-compatible server, e.g. vLLM)
                         LLM_BASE_URL=http://server:8000/v1  OPENAI_API_KEY=anything
  LLM_PROVIDER=dummy     (tests/demos; always answers LLM_DUMMY_RESPONSE)
"""
import os

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel

load_dotenv()


def get_chat_model() -> BaseChatModel:
    provider = os.getenv("LLM_PROVIDER", "groq").strip().lower()
    model_name = os.getenv("LLM_MODEL", "openai/gpt-oss-120b").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip() or None

    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=model_name, temperature=0)

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model_name, temperature=0, base_url=base_url)

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model_name,
            temperature=0,
            base_url=base_url,
            api_key=os.getenv("OPENAI_API_KEY", "not-needed"),
        )

    if provider == "dummy":
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        return FakeListChatModel(responses=[os.getenv("LLM_DUMMY_RESPONSE", "{}")])

    raise ValueError(f"Unsupported LLM_PROVIDER: {provider!r}")
