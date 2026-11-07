---
name: rps-input-diagnosis
description: Diagnose RPS input conflicts, failed constraint evidence, and successful property bounds with the offline rps_tools CLI. Use for input infeasibility, forced loads, feed and property restrictions, failure rows, or property binding questions; distinguish proofs from clues and preserve the original solver result.
---

# RPS diagnosis in rps_tools

Use the tools in this repository. They read saved files and never run the business solver. Do not import another checkout of rps_solver or automatically launch sensitivity solves.

## Select the evidence

- Raw input: `python -m rps_tools run diagnose.input_feasibility input=<file>`.
  Add `snapshot=<file>` only for a pre-simplification `{bounds,rows}` model snapshot with effective bound semantics.
- Failed output: `python -m rps_tools run diagnose.failure_analysis output=<file> input=<file>`.
  It uses embedded diagnostics first. Optional `model`, `native`, `trace`, and `row_map` files add saved original-space evidence; see [artifact-contract.md](references/artifact-contract.md).
- Successful output and property limits: `python -m rps_tools run diagnose.property_bounds output=<file> input=<file>`.
  If the output lacks embedded property diagnostics, provide `model=<same-run solved MPProto JSON>`.
- Successful flows, dumping, economics, or two scenarios: use existing `diagnose.material_trace`, `diagnose.scan_export`, `diagnose.compare_scenarios`, or `exp.analyze`. Inspect parameters with `python -m rps_tools tools <id>`.

Append `--out <job-directory>` to save reports in an explicit location. Web tools use the same implementation. No Redis or solver binary is required.

Identify input versus output before analyzing: `solver_status_code` identifies a business result; `variable/constarint` identifies the legacy model dump. A failed output's fallback/start values are not a valid allocation. Do not auto-pair independent “latest” files. Use explicit files from one run, preferably a SHA256 manifest; reject conflicting run IDs.

## Interpret findings

- `confirmed_conflicts`: exact model-bound/linear-necessary-condition evidence, rechecked using rational arithmetic. Inspect source scope and any omitted rows.
- `reported_conflicts`: evidence imported from an existing output, with its original confidence. It has not been independently re-certified by this run.
- `input_hints` and `suspected_constraints`: candidates, never a proof of global nonlinear infeasibility. Native residuals describe an observed iterate, possibly an intermediate point, not a necessary relaxation amount.
- No finding means incomplete coverage, not proof that the whole model can solve.
- A controlled input change that succeeds identifies an effective remedy; alone it does not prove that the original model has no feasible solution or that the changed constraint is the unique cause.

Explain the shortest supported chain: positive lower bound → required case/feed → available supply/path/property → actual contradiction or ranked doubt. Cite exact input paths and effective bounds separately. Preserve solver termination, solution availability, OBJ, multistart objectives and stability; never turn a diagnostic into a new solver status or a postsolve acceptance gate.

Use [checklist.md](references/checklist.md) when tracing a new input. Use [artifact-contract.md](references/artifact-contract.md) for missing or mismatched evidence. Keep real customer examples and original migrated case notes in `private/diagnosis_migration/`; only synthetic examples belong in public code, tests and references.
