"""Plain-Python nodes for the claim workflow."""

import json
import os
import uuid
from datetime import date

from google.adk import Context, Event
from google.adk.events import RequestInput
from google.adk.workflow import NodeTimeoutError, RetryConfig, node
from pydantic import BaseModel, ValidationError

from .data import (AUTO_APPROVE_LIMIT, CLAIMS, FRAUD_REVIEW_SCORE, LARGE_CLAIM,
                   POLICIES)
from .schemas import (FIELD_LABELS, REQUIRED_FIELDS, AdjusterDecision,
                      ClaimFacts, CoverageResult, DamageEstimate, FraudResult,
                      LetterReview, Triage, rupiah)

MAX_INFO_REQUESTS = 2
MAX_LETTER_ROUNDS = 3


def _as_dict(value):
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def route_request(node_input):
    triage = Triage.model_validate(node_input)
    return Event(route=triage.kind, output=triage.text)


def status_lookup(node_input: str):
    text = node_input.upper()
    claim_id = next((cid for cid in CLAIMS if cid in text), None)
    if claim_id is None:
        return Event(message="Sorry, we couldn't find a claim number (e.g. CLM-0042)"
                             " in your message. Could you send it?")
    claim = CLAIMS[claim_id]
    return Event(message=f"{claim_id}: {claim['status']}. {claim['detail']}")


def validate_claim(node_input, attempts: int = 0):
    claim = ClaimFacts.model_validate(node_input)
    missing = [f for f in REQUIRED_FIELDS if getattr(claim, f) in (None, "")]
    state = {"claim": claim.model_dump()}

    # reset the counter once we're done so the next claim starts fresh
    if not missing:
        return Event(route="OK", output=claim, state={**state, "attempts": 0})
    if attempts >= MAX_INFO_REQUESTS:
        return Event(route="MANUAL", output=claim, state={**state, "attempts": 0})
    return Event(route="ASK", output=missing,
                 state={**state, "attempts": attempts + 1})


def ask_customer(node_input: list[str]):
    fields = ", ".join(FIELD_LABELS[f] for f in node_input)
    yield RequestInput(
        message=(f"To open your claim we still need: {fields}."
                 ' Reply in JSON, e.g. {"policy_id": "POL-2002",'
                 ' "incident_date": "2026-11-08"}'),
        payload={"missing": node_input},
        response_schema=ClaimFacts,
    )


def merge_reply(node_input, claim: dict):
    reply = _as_dict(node_input)
    for key, value in reply.items():
        if key not in ClaimFacts.model_fields or value in (None, ""):
            continue
        try:
            claim = ClaimFacts(**{**claim, key: value}).model_dump()
        except ValidationError:
            pass  # bad value, leave it out so validate_claim asks again
    return Event(output=ClaimFacts(**claim))


def manual_queue(node_input):
    return Event(message="Thank you. A claims officer will contact you within"
                         " 1 working day to complete your claim details.")


def start_assessment(node_input):
    yield Event(message="Claim details complete. Running fraud, coverage and"
                        " damage checks...")
    yield Event(output=node_input)


class FraudServiceUnavailable(Exception):
    pass


@node(
    retry_config=RetryConfig(max_attempts=3, initial_delay=0.5,
                             exceptions=[FraudServiceUnavailable, NodeTimeoutError]),
    timeout=10.0,
)
async def fraud_check(ctx: Context, node_input):
    yield Event(message=f"Calling fraud service (attempt {ctx.attempt_count})")

    # fake an outage on the first call to show the retry; CLAIMFLOW_FLAKY=0 turns it off
    if os.getenv("CLAIMFLOW_FLAKY", "1") == "1" and ctx.attempt_count == 1:
        raise FraudServiceUnavailable("fraud service timed out")

    claim = ClaimFacts.model_validate(node_input)
    policy = POLICIES.get(claim.policy_id or "", {})
    incident = _parse_date(claim.incident_date)
    score, flags = 0.05, []

    if policy and incident:
        days = (incident - date.fromisoformat(policy["start"])).days
        if 0 <= days <= 30:
            score += 0.45
            flags.append(f"loss occurred {days} days after the policy started")
    if (claim.amount_claimed or 0) > LARGE_CLAIM:
        score += 0.2
        flags.append(f"claimed amount above {rupiah(LARGE_CLAIM)}")

    yield Event(output=FraudResult(score=round(min(score, 1.0), 2), flags=flags))


def coverage_check(node_input):
    claim = ClaimFacts.model_validate(node_input)
    policy = POLICIES.get(claim.policy_id or "")

    if not policy:
        result = CoverageResult(covered=False, reason="policy number not found")
    elif policy["status"] != "active":
        result = CoverageResult(covered=False, reason=f"policy is {policy['status']}")
    elif claim.incident_type not in policy["types"]:
        result = CoverageResult(covered=False, reason=f"{policy['product']} does not"
                                f" cover {claim.incident_type}")
    elif claim.incident_cause not in policy["perils"]:
        extra = " (requires the flood extension)" if claim.incident_cause == "flood" else ""
        result = CoverageResult(covered=False, reason=f"{claim.incident_cause} is"
                                f" not covered by {policy['product']}{extra}")
    else:
        result = CoverageResult(covered=True, own_risk=policy["own_risk"],
                                own_risk_pct=policy["own_risk_pct"],
                                limit=policy["limit"],
                                reason="policy active and peril covered")
    return Event(output=result)


def decide(node_input: dict, claim: dict):
    # node_input comes from the join: {branch name: branch output}
    fraud = FraudResult.model_validate(node_input["fraud_check"])
    cover = CoverageResult.model_validate(node_input["coverage_check"])
    damage = DamageEstimate.model_validate(node_input["damage_estimator"])

    claimed = claim.get("amount_claimed") or 0
    loss = min(damage.estimate, claimed, cover.limit)
    own_risk = max(cover.own_risk, cover.own_risk_pct * loss)
    payable = max(0.0, loss - own_risk)

    assessment = {"fraud": fraud.model_dump(), "coverage": cover.model_dump(),
                  "damage": damage.model_dump(), "own_risk": round(own_risk),
                  "payable": round(payable)}

    if not cover.covered:
        route = "DENY"
    elif fraud.score >= FRAUD_REVIEW_SCORE or claimed > AUTO_APPROVE_LIMIT:
        route = "REVIEW"
    else:
        route = "AUTO"

    # adjuster=None so a decision from an earlier claim in the session isn't reused
    return Event(route=route, output=assessment,
                 state={"assessment": assessment, "adjuster": None})


def adjuster_review(node_input: dict, claim: dict):
    fraud = node_input["fraud"]
    flags = "; ".join(fraud["flags"]) or "no flags"
    name = claim.get("claimant") or "customer"
    yield RequestInput(
        message=(f"Adjuster approval needed for {name} ({claim.get('policy_id')})."
                 f" Proposed payment {rupiah(node_input['payable'])} after own risk"
                 f" {rupiah(node_input['own_risk'])}. Fraud score {fraud['score']}"
                 f" ({flags})."
                 ' Reply in JSON: {"approved": true, "approved_amount": 95000000,'
                 ' "note": "..."}'),
        payload=node_input,
        response_schema=AdjusterDecision,
    )


def after_review(node_input):
    decision = AdjusterDecision.model_validate(_as_dict(node_input))
    route = "APPROVED" if decision.approved else "REJECTED"
    return Event(route=route, output=decision,
                 state={"adjuster": decision.model_dump()})


def pay_claim(ctx: Context):
    assessment = ctx.state["assessment"]
    adjuster = ctx.state.get("adjuster") or {}
    amount = adjuster.get("approved_amount") or assessment["payable"]
    outcome = {
        "result": "PAID",
        "amount_idr": round(amount),
        "amount_text": rupiah(amount),
        "payment_id": "PAY-" + uuid.uuid4().hex[:6].upper(),
        "approved_by": "adjuster" if adjuster else "automatic rules",
    }
    return Event(output=outcome,
                 state={"outcome": outcome, "letter_rounds": 0, "letter_issues": ""})


def deny_claim(ctx: Context):
    assessment = ctx.state["assessment"]
    adjuster = ctx.state.get("adjuster")
    if adjuster:
        reason = adjuster.get("note") or "the claim was not approved after adjuster review"
    else:
        reason = assessment["coverage"]["reason"]
    outcome = {"result": "DENIED", "reason": reason}
    return Event(output=outcome,
                 state={"outcome": outcome, "letter_rounds": 0, "letter_issues": ""})


def after_critic(node_input, letter_rounds: int = 0):
    review = LetterReview.model_validate(node_input)
    rounds = letter_rounds + 1
    if review.passed:
        return Event(route="PASS", state={"letter_rounds": rounds})
    if rounds >= MAX_LETTER_ROUNDS:
        return Event(route="ESCALATE", state={"letter_rounds": rounds})
    return Event(route="REVISE", state={"letter_rounds": rounds,
                                        "letter_issues": "; ".join(review.issues)})


def send_letter(ctx: Context):
    yield Event(message=ctx.state["letter"])
    yield Event(output={"outcome": ctx.state["outcome"], "letter_sent": True})


def escalate_letter(ctx: Context):
    yield Event(message=f"Letter still failed review after {MAX_LETTER_ROUNDS} rounds,"
                        " sent to a human editor.")
    yield Event(output={"outcome": ctx.state["outcome"], "letter_sent": False})
