"""
Anthropic (Claude) LLM wrapper.
"""

import os


class AnthropicLLM:
    """Wrapper for Anthropic Claude models."""

    MODEL_PREFIX = "anthropic/"

    # Current Claude models (Opus 4.8/4.7 and Fable 5 family) reject sampling
    # parameters such as `temperature` with a 400 error — adaptive thinking is
    # always on. Only send `temperature` to models that still accept it.
    NO_SAMPLING_PARAMS = (
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-fable-5",
        "claude-mythos-5",
    )

    @staticmethod
    def is_supported(model_name: str) -> bool:
        """Check if this provider supports the given model."""
        name = model_name.lower()
        return "claude" in name or name.startswith(AnthropicLLM.MODEL_PREFIX)

    @staticmethod
    def initialize(model: str, temperature: float, max_tokens: int):
        """Initialize Anthropic client."""
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set.\n"
                "Set it in your environment for Anthropic (Claude) models.\n"
                "Get a key at https://console.anthropic.com/settings/keys"
            )

        # Import the official Anthropic SDK
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic is not installed.\n"
                "Install it with: pip install anthropic"
            )

        # Remove the optional "anthropic/" prefix if present
        model_name = model.replace(AnthropicLLM.MODEL_PREFIX, "", 1)

        # Create wrapper class
        class AnthropicWrapper:
            def __init__(self, model, temperature, max_tokens):
                self.model = model
                self.temperature = temperature
                self.max_tokens = max_tokens
                self.client = anthropic.Anthropic(api_key=api_key)

            def invoke(self, prompt):
                """Call the Anthropic Messages API and return the response."""
                request_kwargs = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                }
                # Only send `temperature` to models that accept it; newer models
                # reject sampling parameters with a 400.
                rejects_sampling = any(
                    self.model.startswith(prefix)
                    for prefix in AnthropicLLM.NO_SAMPLING_PARAMS
                )
                if not rejects_sampling:
                    request_kwargs["temperature"] = self.temperature

                response = self.client.messages.create(**request_kwargs)

                # Concatenate all text blocks (ignore thinking/tool blocks).
                text = "".join(
                    block.text for block in response.content
                    if getattr(block, "type", None) == "text"
                )

                # Create a response object similar to LangChain's
                class Response:
                    def __init__(self, text, usage_metadata):
                        self.content = text
                        self.usage_metadata = usage_metadata

                usage = {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": (
                        response.usage.input_tokens + response.usage.output_tokens
                    ),
                }
                return Response(text, usage)

        return AnthropicWrapper(model_name, temperature, max_tokens)
