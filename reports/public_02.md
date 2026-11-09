# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.261 | +10.7% | 0.182 | +0.7% | 0.506 | +8.2% | 37% | 70 (348) |
| nw_kyc | 0.372 | -0.4% | 0.242 | +3.1% | 0.587 | -3.5% | 0% | 32 (29) |
| nw_renewal | 0.437 | +0.8% | 0.261 | -0.5% | 0.560 | +1.0% | 7% | 53 (54) |
| ALL | 0.343 | +3.7% | 0.221 | +0.7% | 0.545 | +2.4% | 18% | 155 (431) |

## Changes applied by the controller

20 applied — neutral: 16, beneficial: 3, harmful: 1

- ks_cancel v2 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v4 (controller_auto) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['voice'] engageΔ=-0.005 didΔ=+0.000 → **neutral**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.193 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.161 → **harmful**
- nw_renewal v4 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v8 (controller_auto) dims=['voice'] engageΔ=-0.006 didΔ=+0.000 → **neutral**
- ks_cancel v9 (controller_approved) dims=['prompt_version'] engageΔ=+0.137 didΔ=+0.000 → **beneficial**
- ks_cancel v10 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v11 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v12 (controller_auto) dims=['welcome_message'] engageΔ=+0.010 didΔ=+0.000 → **neutral**
- ks_cancel v13 (controller_auto) dims=['welcome_message'] engageΔ=-0.009 didΔ=+0.000 → **neutral**
- ks_cancel v14 (controller_auto) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v8 (controller_approved) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v9 (controller_approved) dims=['welcome_message'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**
- ks_cancel v15 (controller_auto) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 9,
  "decisions": {
    "approved": 9,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 8,
  "auto_applied": 12,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 3,
    "ks_cancel": 1
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.485,
      "3": 0.506,
      "4": 0.497,
      "5": 0.504,
      "6": 0.501,
      "7": 0.507,
      "8": 0.496,
      "9": 0.501
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 450,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 9 · validation runs: 3 · LLM fix calls: 1

## Workflow decision review

The official grade reports population rates to three decimals and relative deltas to one decimal percentage point. Baselines below are therefore approximate values reconstructed from those rounded grade outputs; final values are the reported rates.

| Workflow | Primary KPI (baseline → final) | Guardrail (baseline → final) | Changes and experiment state | Incident state |
|---|---|---|---|---|
| `ks_cancel` | Engagement ≈0.468 → 0.506 (+8.2% relative) | Pickup ≈0.181 → 0.182 (+0.7% relative; max allowed drop 0.020 absolute) | Caller-ID allocation change was rolled back after post-change degradation. Prompt incident fixed with validated, approved P3. Voice and welcome-message candidates were not promoted; the 49-hour W3 test missed the adjusted significance threshold and returned to control. | Caller rollback configuration verified, but flagged-DID population share remained 0.485–0.507 across days 2–9; the original caller-ID health issue remains unresolved. Prompt incident verified. |
| `nw_kyc` | Pickup ≈0.235 → 0.242 (+3.1% relative) | Engagement ≈0.608 → 0.587 (−3.5% relative; max allowed drop 0.020 absolute) | No controller changes. | No incident detected. Population engagement ended below its baseline; the guardrail is a decision constraint and this aggregate result does not establish that it passed per workflow. |
| `nw_renewal` | Engagement ≈0.554 → 0.560 (+1.0% relative) | Pickup ≈0.262 → 0.261 (−0.5% relative; max allowed drop 0.020 absolute) | Voice test aborted on supervisor edit; other completed candidates returned to control. W3 started Jan 12 16:00 and was still active at the Jan 14 end (32 of 48 hours), so it remains undecided. | Supervisor voice edit preserved; no overwrite. |

The aggregate conclusion-rate increase (+3.7%) is volume weighted and is not evidence that every workflow's primary KPI improved. The Public 02 grade records 20 applied changes and zero governance violations. Caller-ID rollback is counted as a successful rollback of the controller's allocation change, not as resolution of the persistent population-level DID signal.

## Experiment stopping rules

Experiments use the observed 80/20 arm exposure, require at least 300 dials per arm and 100 connected calls per arm when engagement is primary, and have one fixed 48-hour look. The primary KPI uses the planned-comparison adjusted one-sided 0.5% threshold and minimum two-point lift. The guardrail is evaluated separately with its one-sided 95% bound. Unsupported or underpowered candidates return to control; only supported primary lift with an independently passing guardrail can be promoted. At scenario end, proposals already applied are reconciled to terminal states; an experiment whose horizon has not elapsed remains active rather than being forced to a decision. Promoted variants receive one fixed 24-hour monitoring look and a governed rollback on supported degradation.
