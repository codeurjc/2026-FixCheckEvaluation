"""
Google Generative AI LLM wrapper.
"""

import os


class GoogleLLM:
    """Wrapper for Google Generative AI models."""
    
    @staticmethod
    def is_supported(model_name: str) -> bool:
        """Check if this provider supports the given model."""
        return (
            "gemini" in model_name.lower() or 
            "gemma" in model_name.lower() or 
            model_name.startswith("google/")
        )
    
    @staticmethod
    def initialize(model: str, temperature: float, max_tokens: int):
        """Initialize Google Generative AI client."""
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError(
                "GOOGLE_API_KEY is not set.\n"
                "Set it in your environment for Google models.\n"
                "Get a key at https://aistudio.google.com/app/apikey"
            )
        
        # Remove "google/" prefix and ":free" suffix if present
        model_name = model.replace("google/", "").replace(":free", "")
        
        # Import google-generativeai
        try:
            from google import generativeai as genai
        except ImportError:
            raise ImportError(
                "google-generativeai is not installed.\n"
                "Install it with: pip install google-generativeai"
            )
        
        # Configure API
        genai.configure(api_key=api_key)
        
        # Create wrapper class
        class GoogleWrapper:
            def __init__(self, model, temperature, max_tokens):
                self.model = genai.GenerativeModel(model)
                self.temperature = temperature
                self.max_tokens = max_tokens
            
            def invoke(self, prompt):
                generation_config = {
                    "temperature": self.temperature,
                    "max_output_tokens": self.max_tokens,
                }
                response = self.model.generate_content(
                    prompt,
                    generation_config=generation_config
                )
                
                # Create a response object similar to LangChain's
                class Response:
                    def __init__(self, text):
                        self.content = text
                        self.usage_metadata = {
                            "input_tokens": 0,  # Google API doesn't always provide this
                            "output_tokens": 0
                        }
                
                return Response(response.text)
        
        return GoogleWrapper(model_name, temperature, max_tokens)
