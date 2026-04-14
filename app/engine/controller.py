from __future__ import annotations

import httpx


class ControllerAgent:
    def __init__(
        self,
        ollama_url: str = "http://host.docker.internal:11434/api/generate",
        model: str = "llama3",
        timeout: float = 30.0,
    ) -> None:
        self.ollama_url = ollama_url
        self.model = model
        self.timeout = timeout

    async def summarize(self, text: str) -> str:
        prompt = (
            "You are a meeting assistant. "
            "Summarize the following transcript text in exactly one sentence.\n\n"
            f"{text.strip()}"
        )

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.ollama_url, json=payload)
            response.raise_for_status()
            data = response.json()

        summary = str(data.get("response", "")).strip()
        if not summary:
            raise RuntimeError("Ollama returned an empty summary")
        return " ".join(summary.split())
