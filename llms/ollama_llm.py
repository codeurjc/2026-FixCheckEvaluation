"""
Ollama LLM wrapper.
"""

import os

class OllamaLLM:
    """Wrapper for Ollama models."""
    
    @staticmethod
    def is_supported(model_name: str) -> bool:
        """Check if this provider supports the given model."""
        # Support models with ollama/ prefix
        return model_name.startswith("ollama/")
    
    @staticmethod
    def initialize(model: str, temperature: float, max_tokens: int):
        """Initialize Ollama client.

        Args:
            model: Model identifier (optionally prefixed with "ollama/").
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate (-1 = unlimited)
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

        # Create wrapper class
        class OllamaWrapper:
            def __init__(self, model, host, temperature, max_tokens):
                self.model = model
                self.host = host
                self.temperature = temperature
                self.max_tokens = max_tokens
                self.client = Client(host=host)

            def invoke(self, prompt):
                """
                Call Ollama API to generate a response.
                """
                try:
                    # Use -1 for num_predict to let Ollama generate unlimited tokens
                    # (it will stop naturally when the response is complete).
                    # This prevents premature truncation of responses.
                    chat_kwargs = {
                        "model": self.model,
                        "messages": [{'role': 'user', 'content': prompt}],
                        "keep_alive": -1,  # keep model in GPU memory indefinitely
                        "options": {
                            "temperature": self.temperature,
                            "num_ctx": 49152,  # Max context window (must fit prompt + max_tokens)
                            "num_predict": self.max_tokens  # -1 = unlimited, let model decide when to stop
                        }
                    }

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
        
        return OllamaWrapper(model_name, host, temperature, max_tokens)
