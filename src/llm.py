"""DashScope Chat Completions adapter; one isolated model per role."""
from langchain_openai import ChatOpenAI
from src.config import Settings


def build_model(settings: Settings, role: str):
    prefix = {'main': 'main', 'analyst': 'analyst', 'report_designer': 'report'}[role]
    extra = {'enable_thinking': getattr(settings, prefix + '_enable_thinking')}
    if prefix == 'analyst' and extra['enable_thinking']:
        extra['thinking_budget'] = settings.analyst_thinking_budget
    return ChatOpenAI(model=getattr(settings, prefix + '_model'), api_key=settings.api_key,
                      base_url=settings.base_url, use_responses_api=False,
                      max_tokens=getattr(settings, prefix + '_max_output_tokens'),
                      extra_body=extra, timeout=settings.model_timeout_seconds,
                      max_retries=1, temperature=0.1,
                      streaming=getattr(settings, 'model_streaming_enabled', False),
                      stream_usage=True if getattr(settings, 'model_streaming_enabled', False) else None)
