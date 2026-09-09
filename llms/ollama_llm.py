"""
Ollama LLM wrapper.
"""

import os

# Generation budget. Both are hard ceilings the daemon enforces silently: when
# either is reached the model simply stops, and a reasoning model that has not
# finished thinking yet stops *before writing any answer at all*. The wrapper
# therefore reports which ceiling it hit instead of returning an empty string
# that reads downstream as "the model had nothing to say".
#
# The real default lives in FixGenerator.DEFAULT_CONTEXT_LENGTH, which is what
# callers pass; this is only the fallback for a direct use of this module.
# NOTE: the daemon must serve at least this much -- scripts/ollama_serve.sh sets
# OLLAMA_CONTEXT_LENGTH, and a per-request num_ctx larger than the model was
# loaded with is silently clamped. Keep the two in step.
DEFAULT_NUM_CTX = 131072  # prompt + generation, together


class OllamaLLM:
    """Wrapper for Ollama models."""

    @staticmethod
    def is_supported(model_name: str) -> bool:
        """Check if this provider supports the given model."""
        # Support models with ollama/ prefix
        return model_name.startswith("ollama/")

    @staticmethod
    def initialize(model: str, temperature: float, max_tokens: int,
                   context_length: int = DEFAULT_NUM_CTX):
        """Initialize Ollama client.

        Args:
            model: Model identifier (optionally prefixed with "ollama/").
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate, passed as ``num_predict``.
                Ollama treats ``-1`` as unlimited, but callers pass a real
                ceiling, and reaching it truncates the generation mid-stream.
                For a reasoning model this counts the chain of thought too, so
                the ceiling can be spent before any answer is written.
            context_length: ``num_ctx`` -- prompt and generation together. The
                daemon must have loaded the model with at least this much.
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
            def __init__(self, model, host, temperature, max_tokens, num_ctx):
                self.model = model
                self.host = host
                self.temperature = temperature
                self.max_tokens = max_tokens
                self.num_ctx = num_ctx
                self.client = Client(host=host)

            def invoke(self, prompt):
                """
                Call Ollama API to generate a response.
                """
                chat_kwargs = {
                    "model": self.model,
                    "messages": [{'role': 'user', 'content': prompt}],
                    "keep_alive": -1,  # keep model in GPU memory indefinitely
                    "options": {
                        "temperature": self.temperature,
                        "num_ctx": self.num_ctx,
                        "num_predict": self.max_tokens,
                    }
                }

                try:
                    response = self.client.chat(**chat_kwargs)
                except Exception as e:
                    # Only the call itself is wrapped as a connectivity problem.
                    # Anything raised while *reading* the response used to end up
                    # here too, reporting a parse bug as "Ollama is not running".
                    raise RuntimeError(
                        f"Failed to connect to Ollama at {self.host}.\n"
                        f"Make sure Ollama is running and accessible.\n"
                        f"Error: {str(e)}"
                    )

                return build_response(
                    response, num_ctx=self.num_ctx, max_tokens=self.max_tokens,
                    model=self.model,
                )

        return OllamaWrapper(model_name, host, temperature, max_tokens,
                             context_length)


class Response:
    """A generation, plus what the daemon said about how it ended.

    ``content`` alone is not enough to tell a model that answered nothing from
    one that was cut off mid-thought: both are the empty string. The extra
    fields exist so the caller can record the difference rather than score a
    truncated run as a failed repair. 46 runs of the first campaign did exactly
    that -- 30 stopped at ``num_predict``, 16 exhausted ``num_ctx`` -- and all
    46 were reported as the model failing to fix the bug.
    """

    def __init__(self, text, prompt_tokens, response_tokens, *, done_reason="",
                 thinking_chars=0, truncated=False, context_exhausted=False):
        self.content = text
        self.usage_metadata = {
            "input_tokens": prompt_tokens,
            "output_tokens": response_tokens,
            "total_tokens": prompt_tokens + response_tokens,
        }
        self.done_reason = done_reason
        self.thinking_chars = thinking_chars
        self.truncated = truncated
        self.context_exhausted = context_exhausted

    @property
    def empty(self):
        return not (self.content or "").strip()

    def generation_status(self):
        """A short, recordable reason -- ``"ok"`` when nothing went wrong."""
        if self.context_exhausted:
            return "context_exhausted"
        if self.truncated:
            return "truncated"
        if self.empty:
            return "empty_response"
        return "ok"


def build_response(response, *, num_ctx, max_tokens, model=""):
    """Turn an Ollama ``ChatResponse`` into a :class:`Response`, loudly.

    Reasoning models put their chain of thought in ``message.thinking`` and the
    answer in ``message.content``. Reading only ``content`` is correct -- the
    thinking is not an answer and must never be passed off as one -- but when
    it comes back empty while tokens were generated, that is a fact about the
    *harness*, not about the model's ability, and it has to be visible.
    """
    message = getattr(response, "message", None)
    text = getattr(message, "content", None) or ""
    thinking = getattr(message, "thinking", None) or ""
    prompt_tokens = getattr(response, "prompt_eval_count", None) or 0
    response_tokens = getattr(response, "eval_count", None) or 0
    done_reason = getattr(response, "done_reason", None) or ""

    # Two independent ceilings, checked separately because the remedy differs:
    # a longer num_predict fixes one, a bigger context window the other.
    truncated = (done_reason == "length") or (
        max_tokens > 0 and response_tokens >= max_tokens
    )
    context_exhausted = (prompt_tokens + response_tokens) >= num_ctx

    built = Response(
        text, prompt_tokens, response_tokens,
        done_reason=done_reason, thinking_chars=len(thinking),
        truncated=truncated, context_exhausted=context_exhausted,
    )

    status = built.generation_status()
    if status != "ok":
        where = f" from {model}" if model else ""
        print(
            f"[ollama] WARNING: generation{where} ended as {status!r} "
            f"(done_reason={done_reason!r}, prompt={prompt_tokens} tok, "
            f"generated={response_tokens} tok of max {max_tokens}, "
            f"num_ctx={num_ctx}, reasoning={len(thinking)} chars, "
            f"answer={len(text)} chars).",
            flush=True,
        )
        if built.empty and thinking:
            print(
                "[ollama] The model spent its whole budget reasoning and never "
                "emitted an answer. This is a budget problem, not a failed "
                "repair -- do not read the empty patch as the model giving up.",
                flush=True,
            )
    return built
