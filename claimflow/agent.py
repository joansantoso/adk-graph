"""ClaimFlow workflow graph. Diagram is in README.md."""

from google.adk import Workflow
from google.adk.workflow import JoinNode

from .agents import (damage_estimator, intake_agent, letter_critic,
                     letter_writer, triage_agent)
from .nodes import (adjuster_review, after_critic, after_review, ask_customer,
                    coverage_check, decide, deny_claim, escalate_letter,
                    fraud_check, manual_queue, merge_reply, pay_claim,
                    route_request, send_letter, start_assessment,
                    status_lookup, validate_claim)

gather = JoinNode(name="gather")

root_agent = Workflow(
    name="claimflow",
    edges=[
        ("START", triage_agent, route_request),
        (route_request, {"NEW_CLAIM": intake_agent, "STATUS": status_lookup}),

        # keep asking the customer until the claim is complete (or give up)
        (intake_agent, validate_claim),
        (validate_claim, {"OK": start_assessment,
                          "ASK": ask_customer,
                          "MANUAL": manual_queue}),
        (ask_customer, merge_reply, validate_claim),

        # fraud, coverage and damage run in parallel
        (start_assessment, (fraud_check, coverage_check, damage_estimator),
         gather, decide),

        (decide, {"AUTO": pay_claim, "REVIEW": adjuster_review,
                  "DENY": deny_claim}),
        (adjuster_review, after_review),
        (after_review, {"APPROVED": pay_claim, "REJECTED": deny_claim}),

        # letter goes back to the writer until the critic is happy
        (pay_claim, letter_writer),
        (deny_claim, letter_writer),
        (letter_writer, letter_critic, after_critic),
        (after_critic, {"PASS": send_letter,
                        "REVISE": letter_writer,
                        "ESCALATE": escalate_letter}),
    ],
)
