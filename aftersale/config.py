import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


@dataclass
class Settings:
    runtime_dir: Path
    mode: str = "demo"
    max_steps: int = 90
    agent_steps: int = 14
    max_delegations: int = 6
    retries: int = 1
    tool_timeout: float = 3.0
    proposal_ttl: int = 900
    rag_url: str = ""
    allow_model: bool = False
    model_call_limit: int = 40

    @classmethod
    def from_env(cls):
        return cls(
            runtime_dir=Path(os.getenv("AFTERSALE_RUNTIME_DIR", "../aftersale-runtime")),
            mode=os.getenv("AFTERSALE_MODE", "demo"),
            rag_url=os.getenv("AFTERSALE_RAG_URL", ""),
            allow_model=os.getenv("AFTERSALE_ALLOW_MODEL", "false").lower() == "true",
            model_call_limit=int(os.getenv("AFTERSALE_MODEL_CALL_LIMIT", "40")),
        )

    def prepare(self):
        self.runtime_dir = self.runtime_dir.resolve()
        public = PACKAGE.parent
        if self.runtime_dir == public or public in self.runtime_dir.parents:
            raise ValueError("Runtime directory must be outside the public project")
        if self.mode not in {"demo", "test", "model"}:
            raise ValueError("Unknown runtime mode")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.mode == "model" and not self.allow_model:
            raise ValueError("Model calls require explicit AFTERSALE_ALLOW_MODEL=true")
        return self
