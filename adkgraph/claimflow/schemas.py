"""Pydantic models passed between nodes."""

from typing import Literal

from pydantic import BaseModel, Field


class Triage(BaseModel):
    kind: Literal["NEW_CLAIM", "STATUS"]
    text: str = Field(description="The customer's message, unchanged.")


# all optional so validate_claim can tell what's missing
class ClaimFacts(BaseModel):
    claimant: str | None = Field(None, description="Customer name")
    policy_id: str | None = Field(None, description="Policy number, e.g. POL-1001")
    incident_type: Literal["vehicle", "property", "travel", "other"] | None = None
    incident_cause: Literal["collision", "theft", "flood", "fire", "other"] | None = None
    incident_date: str | None = Field(None, description="Incident date, format YYYY-MM-DD")
    description: str | None = None
    amount_claimed: float | None = Field(None, description="Claimed amount in Rupiah (number only)")


REQUIRED_FIELDS = ["policy_id", "incident_type", "incident_cause",
                   "incident_date", "amount_claimed"]

FIELD_LABELS = {
    "policy_id": "policy number",
    "incident_type": "insured object (vehicle/property/travel)",
    "incident_cause": "cause of loss",
    "incident_date": "incident date",
    "amount_claimed": "estimated loss amount (Rp)",
}


class FraudResult(BaseModel):
    score: float = Field(ge=0, le=1)
    flags: list[str] = []


class CoverageResult(BaseModel):
    covered: bool
    own_risk: float = 0        # risiko sendiri, fixed Rp per incident
    own_risk_pct: float = 0    # or a share of the loss, e.g. 0.10
    limit: float = 0           # harga pertanggungan
    reason: str


class DamageEstimate(BaseModel):
    estimate: float = Field(description="Fair repair/replacement cost in Rupiah")
    confidence: float = Field(ge=0, le=1)
    rationale: str


class AdjusterDecision(BaseModel):
    approved: bool
    approved_amount: float | None = None
    note: str = ""


class LetterReview(BaseModel):
    passed: bool
    issues: list[str] = []


def rupiah(amount: float) -> str:
    """8500000 -> 'Rp 8.500.000'"""
    return "Rp " + f"{amount:,.0f}".replace(",", ".")
