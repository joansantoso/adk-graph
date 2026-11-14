"""In-memory demo data. Products and numbers are made up, not real tariffs."""

POLICIES = {
    # all-risk car, fixed own risk per incident
    "POL-1001": {"holder": "Budi Santoso", "product": "Comprehensive Car Insurance",
                 "types": ["vehicle"], "perils": ["collision", "theft"],
                 "status": "active", "start": "2024-03-01",
                 "own_risk": 300_000, "own_risk_pct": 0, "limit": 250_000_000},
    # home with flood extension, 10% own risk
    "POL-2002": {"holder": "Siti Rahmawati", "product": "Home Insurance + Flood Extension",
                 "types": ["property"], "perils": ["fire", "flood"],
                 "status": "active", "start": "2026-11-05",
                 "own_risk": 1_000_000, "own_risk_pct": 0.10, "limit": 500_000_000},
    # lapsed motorbike TLO
    "POL-3003": {"holder": "Agus Pratama", "product": "Motorbike Total Loss Only (TLO) Insurance",
                 "types": ["vehicle"], "perils": ["theft"],
                 "status": "inactive (premium unpaid)", "start": "2022-01-15",
                 "own_risk": 0, "own_risk_pct": 0, "limit": 22_000_000},
    # fire only, no flood extension
    "POL-4004": {"holder": "Rina Wijaya", "product": "Standard Fire Insurance",
                 "types": ["property"], "perils": ["fire"],
                 "status": "active", "start": "2025-06-01",
                 "own_risk": 500_000, "own_risk_pct": 0, "limit": 750_000_000},
}

CLAIMS = {
    "CLM-0042": {"status": "Paid", "detail": "Rp 4.750.000 transferred on 28 October 2026."},
    "CLM-0077": {"status": "Under surveyor review", "detail": "Decision within 2 working days."},
}

AUTO_APPROVE_LIMIT = 25_000_000   # above this an adjuster has to sign off
LARGE_CLAIM = 100_000_000         # counts as a fraud signal
FRAUD_REVIEW_SCORE = 0.6
COMPLAINT_CONTACT = "OJK Contact 157"
