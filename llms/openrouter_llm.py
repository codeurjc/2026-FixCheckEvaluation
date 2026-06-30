"""
OpenRouter LLM wrapper.
"""

import os
from langchain.chat_models import init_chat_model


class OpenRouterLLM:
    """Wrapper for OpenRouter models (Meta, DeepSeek, etc.)."""
    
    @staticmethod
    def is_supported(model_name: str) -> bool:
        """
        Check if this provider supports the given model.
        This is the default/fallback provider.
        """
        return True  # Default fallback for all other models
    
    @staticmethod
    def initialize(model: str, temperature: float, max_tokens: int):
        """Initialize OpenRouter client."""
        api_key = os.getenv("OPENROUTER_API_KEY")
        base_url = os.getenv("OPENROUTER_BASE_URL")
        
        if not api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is not set.\n"
                "Set it in your environment or pass it to the constructor.\n"
                "You can get a key at https://openrouter.ai/"
            )
        
        return init_chat_model(
            model_provider="openai",
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens
        )
