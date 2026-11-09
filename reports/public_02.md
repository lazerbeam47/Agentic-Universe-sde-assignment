# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.281 | +19.2% | 0.201 | +10.7% | 0.492 | +5.2% | 24% | 92 (348) |
| nw_kyc | 0.369 | -1.2% | 0.236 | +0.7% | 0.597 | -1.9% | 0% | 25 (29) |
| nw_renewal | 0.437 | +0.8% | 0.264 | +0.7% | 0.553 | -0.2% | 8% | 54 (54) |
| ALL | 0.351 | +6.3% | 0.229 | +4.5% | 0.537 | +0.9% | 13% | 171 (431) |

`ALL` is the simulator's volume-weighted aggregate. It does not mean every workflow improved.

## Workflow primary KPI and guardrail

Baseline rates below are inferred from the grader's rounded final values and relative deltas, so they are approximate to three decimals. Guardrail limits come from each workflow's public policy.

| Workflow | Primary KPI: baseline → final | Guardrail | Guardrail baseline → final | Primary result |
|---|---|---|---|---|
| ks_cancel | Engagement 0.468 → 0.492 (+5.2% relative) | Pickup max drop 0.020 | 0.181 → 0.201 | Improved; guardrail improved |
| nw_kyc | Pickup 0.234 → 0.236 (+0.7% relative) | Engagement max drop 0.020 | 0.608 → 0.597 (−0.011 absolute) | Slightly improved; guardrail change is within limit |
| nw_renewal | Engagement 0.555 → 0.553 (−0.2% relative) | Pickup max drop 0.020 | 0.262 → 0.264 | Slightly lower; guardrail improved |

## Changes, experiments and incident state at scenario end

- `ks_cancel`: three policy-permitted caller-ID allocation reductions moved traffic away from D2; a voice test was rolled back, and a second voice test was stopped after the supervisor prompt edit. The caller-ID verifier found lower post-change engagement. In this run's controller build that ended as `rollback_required` without creating a rollback proposal; the rollback-proposal implementation and integration tests were added afterward and have not been rerun against the simulator.
- `nw_kyc`: no controller configuration changes or experiments were recorded.
- `nw_renewal`: voice and welcome-message experiments produced four completed rollbacks. A pending voice test was aborted when the supervisor edited voice configuration; the abort proposal was applied, but this run's experiment row remained `abort_pending` because applied-abort reconciliation was missing. A voice experiment was also marked `aborted_supervisor_edit`. The W3 welcome-message experiment remains active and undecided.
- The W3 experiment had 1,032 observed candidate calls and 4,068 control calls by the scenario end. It began at `2027-01-12 16:00` and the simulator ended at `2027-01-14 00:00`, only 32 hours later. The fixed 48-hour evaluation horizon had not elapsed, so it had insufficient time to reach a valid decision; it was neither promoted nor rejected. The hardening now records this open horizon and actual exposure in the audit.
- The prompt incident's validated P5 change and approved P1 restoration were both applied. Its incident row remained `rollback_pending` at the end because rollback-incident reconciliation was missing; hardening now reconciles the row against the applied proposal and observed config.

The results above are official grader outputs from the recorded run. That run used the previous one-look z threshold of 2.58 for each sequential candidate test; the hardened controller now applies a Bonferroni threshold across the planned voice and welcome-message candidate comparisons per workflow. The public scenarios have not been rerun under that stricter threshold. Pickup and engagement are evaluated separately, and the weighted aggregate is descriptive rather than evidence that every workflow improved.

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
