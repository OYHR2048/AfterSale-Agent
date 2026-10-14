from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Action = Literal["return_refund", "exchange", "cancel", "manual_ticket"]
Status = Literal[
    "running",
    "waiting_info",
    "waiting_confirmation",
    "executing",
    "completed",
    "failed",
    "cancelled",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskRequest(StrictModel):
    message: str = Field(min_length=1, max_length=2000)
    architecture: Literal["single", "multi"] = "multi"


class HumanResponse(StrictModel):
    kind: Literal["supplement", "modify", "confirm", "reject"]
    message: str = Field(default="", max_length=2000)
    proposal_id: str | None = None
    version: int | None = None


class Decision(StrictModel):
    kind: Literal["tool", "finish", "evidence", "resolution", "ask", "propose", "stop"]
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(default="", max_length=500)


class RetrievedChunk(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    content: str
    score: float = 0
    rank: int | None = None
    source: str = "bm25"
    section: str | None = None
    policy_version: str = "v1"
    metadata: dict[str, str] = Field(default_factory=dict)


class Candidate(StrictModel):
    action: Action
    order_id: str
    amount_cents: int
    shipping_payer: Literal["platform", "buyer", "review"]
    reason: str
    evidence_ids: list[str]
    conditions: list[str] = Field(default_factory=list)


class OrderInput(StrictModel):
    order_id: str = Field(pattern=r"^ORD[0-9]{5}$")


class SearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=500)


class EligibilityInput(OrderInput):
    reason: Literal["quality", "no_reason", "cancel", "logistics", "unknown"]
    proof: str = Field(default="", max_length=500)
    sealed: bool | None = None


class StockInput(StrictModel):
    sku: str = Field(pattern=r"^[A-Z0-9_-]{1,30}$")
