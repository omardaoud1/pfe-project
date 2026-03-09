"""
DockerAgent LLM client — powered by Ollama.

Uses Ollama's local HTTP API (localhost:11434) to run Phi-3-mini with Q4
quantization. This gives ~15 tokens/sec on CPU vs minutes with raw transformers.

Ollama must be running: `ollama serve` (or it auto-starts as a system service).
Model must be pulled:   `ollama pull phi3:mini`

Usage:
    from llm_client import DockerAgentSession

    session = DockerAgentSession()
    print(session.chat("add a service called my-api"))
    print(session.chat("use nginx:alpine"))
"""

import requests

# ---------------------------------------------------------------------------
# Ollama config
# ---------------------------------------------------------------------------
OLLAMA_URL  = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "phi3:mini"

# ---------------------------------------------------------------------------
# System prompt — matches what the model was fine-tuned on
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are DockerAgent, an intelligent assistant that helps manage Docker Compose services "
    "for a monitoring pipeline project. You can add or remove services. When adding, you collect: "
    "service name, image, port, monitoring probe type, volumes (optional), depends_on (optional). "
    "When removing, you ask for double confirmation. You always generate valid docker-compose YAML "
    "with the project network (monitoring_default) and correct labels. "
    "Never guess missing information — always ask."
)


# ---------------------------------------------------------------------------
# chat() — single inference call via Ollama API
# ---------------------------------------------------------------------------
def chat(messages: list[dict], max_new_tokens: int = 512) -> str:
    """
    Send a message list to Ollama and return the assistant reply.

    Args:
        messages: list of {"role": "system"|"user"|"assistant", "content": str}
        max_new_tokens: max tokens to generate

    Returns:
        The assistant's reply as a plain string.
    """
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.3,
            "top_p": 0.9,
            "num_predict": max_new_tokens,
        },
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()["message"]["content"].strip()
    except requests.exceptions.ConnectionError:
        return "Error: Ollama is not running. Start it with: ollama serve"
    except requests.exceptions.Timeout:
        return "Error: Ollama took too long to respond. Try again."
    except Exception as e:
        return f"Error calling Ollama: {e}"


# ---------------------------------------------------------------------------
# DockerAgentSession — multi-turn conversation state
# ---------------------------------------------------------------------------
class DockerAgentSession:
    """
    Stateful conversation session with DockerAgent.

    Each call to .chat() appends the user message and model reply to history,
    so the model always sees the full conversation context.

    Example:
        session = DockerAgentSession()
        reply = session.chat("add a service called my-api")
        # model asks for image
        reply = session.chat("use nginx:alpine")
        # model asks for port, etc.
    """

    def __init__(self):
        self.history: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def chat(self, user_message: str, max_new_tokens: int = 512) -> str:
        """Send a user message, get the assistant reply, update history."""
        self.history.append({"role": "user", "content": user_message})
        reply = chat(self.history, max_new_tokens=max_new_tokens)
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def reset(self):
        """Clear conversation history, keep system prompt."""
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]


# ---------------------------------------------------------------------------
# Quick smoke test — run directly: python llm_client.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    session = DockerAgentSession()
    print("DockerAgent ready (Ollama). Type your message or 'quit' to exit.\n")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit"):
            break
        if not user_input:
            continue
        reply = session.chat(user_input)
        print(f"Agent: {reply}\n")
