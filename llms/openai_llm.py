"""
OpenAI LLM wrapper.
"""

import os
from langchain.chat_models import init_chat_model


class OpenAILLM:
    """Wrapper for OpenAI models."""
    
    @staticmethod
    def is_supported(model_name: str) -> bool:
        """Check if this provider supports the given model."""
        return "gpt" in model_name.lower() or model_name.startswith("openai/")
    
    @staticmethod
    def initialize(model: str, temperature: float, max_tokens: int):
        """Initialize OpenAI client."""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is not set.\n"
                "Set it in your environment for OpenAI models.\n"
                "Get a key at https://platform.openai.com/api-keys"
            )
        
        model_name = model.replace("openai/", "")
        
        return init_chat_model(
            model_provider="openai",
            model=model_name,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens
        )
