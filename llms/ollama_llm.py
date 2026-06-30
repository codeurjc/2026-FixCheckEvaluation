"""
Ollama LLM wrapper.
"""

import os
from pydantic import BaseModel


class CategoryScore(BaseModel):
    """Score and reasoning for a single category."""
    score: int
    reasoning: str


class Understanding(BaseModel):
    """Understanding assessment of the commit."""
    score: int
    description: str


class CommitAnnotation(BaseModel):
    """Structured output model for commit annotation."""
    understanding: Understanding
    bfc: CategoryScore
    bpc: CategoryScore
    prc: CategoryScore
    nfc: CategoryScore
    summary: str


class OllamaLLM:
    """Wrapper for Ollama models."""
    
    @staticmethod
    def is_supported(model_name: str) -> bool:
        """Check if this provider supports the given model."""
        # Support models with ollama/ prefix
        return model_name.startswith("ollama/")
    
    @staticmethod
    def initialize(model: str, temperature: float, max_tokens: int, response_format=None):
        """Initialize Ollama client.

        Args:
            model: Model identifier (optionally prefixed with "ollama/").
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate (-1 = unlimited).
            response_format: Optional structured-output schema (JSON schema dict).
                When None, the model returns free-form text (e.g. a unified diff).
                When set to a schema, Ollama constrains the output to that schema.
                Defaults to the CommitAnnotation schema for backward compatibility
                only when explicitly requested via the string "commit-annotation".
        """
        # Import ollama library
        try:
            from ollama import Client
        except ImportError:
            raise ImportError(
                "ollama is not installed.\n"
                "Install it with: pip install ollama"
            )

        # Get Ollama host from environment or use default
        host = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

        # Remove "ollama/" prefix if present
        model_name = model.replace("ollama/", "")

        # Resolve the response format. The sentinel "commit-annotation" keeps the
        # original structured-output behavior used by LLMCommitAnnotator.
        if response_format == "commit-annotation":
            response_format = CommitAnnotation.model_json_schema()

        # Create wrapper class
        class OllamaWrapper:
            def __init__(self, model, host, temperature, max_tokens, response_format):
                self.model = model
                self.host = host
                self.temperature = temperature
                self.max_tokens = max_tokens
                self.response_format = response_format
                self.client = Client(host=host)

            def invoke(self, prompt):
                """
                Call Ollama API to generate a response.

                If ``response_format`` is set, the output is constrained to that
                schema; otherwise the model returns free-form text.
                """
                try:
                    # Use -1 for num_predict to let Ollama generate unlimited tokens
                    # (it will stop naturally when the response is complete).
                    # This prevents premature truncation of responses.
                    chat_kwargs = {
                        "model": self.model,
                        "messages": [{'role': 'user', 'content': prompt}],
                        "options": {
                            "temperature": self.temperature,
                            "num_ctx": 32768,  # Max context window
                            "num_predict": self.max_tokens  # -1 = unlimited, let model decide when to stop
                        }
                    }
                    if self.response_format is not None:
                        chat_kwargs["format"] = self.response_format

                    response = self.client.chat(**chat_kwargs)
                    
                    # Create a response object similar to LangChain's
                    class Response:
                        def __init__(self, text, prompt_tokens, response_tokens):
                            self.content = text
                            self.usage_metadata = {
                                "input_tokens": prompt_tokens,
                                "output_tokens": response_tokens,
                                "total_tokens": prompt_tokens + response_tokens
                            }
                    
                    # Extract response and token counts
                    response_text = response.message.content
                    prompt_tokens = response.prompt_eval_count if hasattr(response, 'prompt_eval_count') else 0
                    response_tokens = response.eval_count if hasattr(response, 'eval_count') else 0
                    
                    return Response(response_text, prompt_tokens, response_tokens)
                    
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to connect to Ollama at {self.host}.\n"
                        f"Make sure Ollama is running and accessible.\n"
                        f"Error: {str(e)}"
                    )
        
        return OllamaWrapper(model_name, host, temperature, max_tokens, response_format)
