# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.281 | +19.2% | 0.201 | +10.7% | 0.492 | +5.2% | 24% | 92 (348) |
| nw_kyc | 0.369 | -1.2% | 0.236 | +0.7% | 0.597 | -1.9% | 0% | 25 (29) |
| nw_renewal | 0.437 | +0.8% | 0.264 | +0.7% | 0.553 | -0.2% | 8% | 54 (54) |
| ALL | 0.351 | +6.3% | 0.229 | +4.5% | 0.537 | +0.9% | 13% | 171 (431) |

## Changes applied by the controller

16 applied — neutral: 10, beneficial: 5, harmful: 1

- ks_cancel v2 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v4 (controller_auto) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['voice'] engageΔ=-0.005 didΔ=+0.000 → **neutral**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.193 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.160 → **beneficial**
- nw_renewal v4 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.067 → **beneficial**
- ks_cancel v9 (controller_auto) dims=['voice'] engageΔ=-0.006 didΔ=+0.000 → **neutral**
- ks_cancel v10 (controller_approved) dims=['prompt_version'] engageΔ=-0.051 didΔ=+0.000 → **harmful**
- ks_cancel v11 (controller_approved) dims=['prompt_version'] engageΔ=+0.161 didΔ=+0.000 → **beneficial**
- nw_renewal v5 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v8 (controller_approved) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v9 (controller_approved) dims=['welcome_message'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 10,
  "decisions": {
    "approved": 10,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 9,
  "auto_applied": 7,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 3,
    "ks_cancel": 2
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
      "2": 0.359,
      "3": 0.0,
      "4": 0.0,
      "5": 0.0,
      "6": 0.0,
      "7": 0.0,
      "8": 0.0,
      "9": 0.0
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 390,
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

Credits used: 11 · validation runs: 4 · LLM fix calls: 1