# Saved-artifact contract

All paths are selected explicitly through the existing CLI/Web tool parameters. Uploaded JSON cannot grant access to paths mentioned inside it.

- `input`: raw RPS request, optional when inspecting already embedded diagnostics.
- `output`: final business output, with `solver_status_code` and optional `solve_diagnostics`, `infeasibility_diagnostics.failure_analysis`, `property_bound_diagnostics`.
- `snapshot`: `{bounds: {code: {lb, ub, integer, semi_continuous}}, rows: [{code,type,rhs,vars,status}]}`. It must represent the original model before destructive simplification. If FX/LB/UB directives exist, effective bounds must already be materialized with `bound_semantics: "mpproto_validation"`.
- `model`: complete saved MPProto `{variable:[...], constarint:[...]}` (the historical spelling is intentional). For property hints this must be the returned solution, not a pre-solve dump. Variable `value` and `reduced_cost`, row `dual_value` and effective bounds must retain their original meaning. N/X `dual_value` is an expression value, not a hard-row economic dual. Missing recomputation provenance makes the affected marginal unavailable.
- `native`: native solution JSON, either its inner object or a single case wrapper. Activities need explicit `scaled:false`.
- `trace`: saved iteration JSONL with `scaled:false`, profile/pid/iteration/infeas/top_violations. The bounded reader keeps source and truncation information.
- `row_map`: `{native_name: original_model_code}` or `{row_map: {...}}`. Omit only when row names already match exactly. Never guess encoded row names.
- `manifest`: `{run_id: optional, files:{input:{sha256:...}, output:{sha256:...}, ...}}`, covering every selected role. Only digests are read, not embedded paths.

Known conflicting run IDs or digest mismatches stop analysis. Without a complete manifest, explicit pairing is reported as not fully verified. Identical code names alone do not verify run identity.

Old expression text skips some definitions/types, old model dumps omit some variable-type and marginal-provenance metadata, and old business outputs may contain start fallbacks. Missing evidence must be reported, not reconstructed from assumptions. No tool certifies global optimality or changes the original status.
