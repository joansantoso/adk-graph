"""LLM agents used by the workflow."""

from google.adk import Agent

from .data import COMPLAINT_CONTACT
from .schemas import ClaimFacts, DamageEstimate, LetterReview, Triage

MODEL = "gemini-3.8-flash"


triage_agent = Agent(
    name="triage_agent",
    model=MODEL,
    instruction=(
        "You are a customer service officer at an Indonesian insurance company."
        " Customers may write in Bahasa Indonesia. Classify the customer's message."
        " Use STATUS if the customer asks about an existing claim (they may mention a"
        " claim number such as CLM-0042). Use NEW_CLAIM if the customer reports a loss"
        " or damage. Copy the customer's message verbatim into the text field."
    ),
    output_schema=Triage,
)

intake_agent = Agent(
    name="intake_agent",
    model=MODEL,
    instruction=(
        "Extract the claim details from the customer's message, which may be in Bahasa"
        " Indonesia. Leave a field null if the message does not mention it; never guess"
        " the policy number, date, or amount."
        " incident_type: vehicle (car/motorbike, mobil/motor), property (house/shop,"
        " rumah/toko), travel, or other."
        " incident_cause: collision (including being sideswiped/diserempet), theft"
        " (dicuri), flood (banjir), fire (kebakaran), or other."
        " Write dates as YYYY-MM-DD."
        " amount_claimed is a plain Rupiah number without separators: 'Rp 8.500.000'"
        " becomes 8500000 and 'Rp 120 million' (or 'Rp 120 juta/jt') becomes 120000000."
    ),
    output_schema=ClaimFacts,
)

damage_estimator = Agent(
    name="damage_estimator",
    model=MODEL,
    instruction=(
        "You are a loss adjuster in Indonesia. You receive claim data as JSON."
        " Estimate a fair repair or replacement cost in Indonesian Rupiah (IDR), based"
        " on authorised workshop rates and market prices in Indonesia; the estimate may"
        " be lower than the amount claimed. Give a confidence from 0 to 1 and a"
        " one-sentence rationale in English."
    ),
    output_schema=DamageEstimate,
)

letter_writer = Agent(
    name="letter_writer",
    model=MODEL,
    instruction=(
        "Write a short, polite, easy-to-understand letter in English to the customer"
        " about their claim. Claim data: {claim}. Outcome: {outcome}."
        " Address the customer as 'Dear Mr./Ms.' followed by their name, state the claim"
        " outcome and the amount in Rupiah format (example: Rp 8.500.000), use at most"
        " 150 words, and sign off with 'Sincerely, ClaimFlow Claims Team'."
        " Fix the following reviewer issues, if any: {letter_issues?}"
    ),
    output_key="letter",
)

LETTER_RUBRIC = f"""\
1. States the claim outcome (paid or denied) within the first two sentences.
2. States the payment amount in Rupiah (Rp), or the reason for the denial.
3. Explains how to file an objection (using the word "objection") and mentions
   {COMPLAINT_CONTACT} (Indonesia's Financial Services Authority) if the objection
   is not resolved.
4. At most 150 words, polite, and promises nothing beyond the claim outcome."""

letter_critic = Agent(
    name="letter_critic",
    model=MODEL,
    instruction=(
        "You review customer letters at an insurance company. Check this letter"
        f" against the rubric.\n\nRubric:\n{LETTER_RUBRIC}\n\n"
        "Letter:\n{letter}\n\nSet passed to true only if every rule is met;"
        " otherwise list each failed rule in issues."
    ),
    output_schema=LetterReview,
)
