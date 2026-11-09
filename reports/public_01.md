# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.354 | +3.7% | 0.242 | -1.2% | 0.513 | +4.4% | 15% | 53 (57) |
| nw_kyc | 0.361 | -1.5% | 0.242 | +0.1% | 0.592 | -0.9% | 0% | 31 (21) |
| nw_renewal | 0.430 | +3.3% | 0.262 | -0.8% | 0.558 | +3.0% | 6% | 44 (54) |
| ALL | 0.381 | +2.5% | 0.249 | -0.8% | 0.544 | +2.8% | 8% | 128 (132) |

## Changes applied by the controller

10 applied — neutral: 7, beneficial: 3

- ks_cancel v2 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v4 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.010 didΔ=+0.000 → **neutral**
- ks_cancel v5 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v6 (controller_auto) dims=['voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- nw_renewal v4 (controller_approved) dims=['welcome_message'] engageΔ=-0.010 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- ks_cancel v7 (controller_approved) dims=['voice'] engageΔ=+0.067 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 7,
  "decisions": {
    "rejected": 2,
    "approved": 5,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 5,
  "auto_applied": 5,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "ks_cancel": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0