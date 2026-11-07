# Diagnosis checklist

1. Confirm raw input, effective configuration and artifact/run identity.
2. Scan explicit contradictory bounds, fixed custom totals and positive capacity/purchase/sale lower bounds. Check whether settings are enabled and overridden.
3. Expand capacity association and coefficients. Avoid double counting shared or overlapping cases.
4. Intersect case crude permission with purchasable supply. `None` upper bound means absent/unbounded; explicit zero disables supply. Check stocks, transfers, plants and periods before claiming no source exists.
5. For a CDU property, use the assay under that case's `crude_usage`, not a similarly named reporting property. Compare with case `defence`.
6. For multiple feeds, compare the best bounded blend at the required load. Local arithmetic does not resolve global crude competition or nonlinear recursion.
7. Check strict utility balances early: forced demand with no purchase or internal source can conflict. Check relaxation flags and internal generation before drawing conclusions.
8. Trace coproduct outlets, sales, downstream capacity, splitter route ratios and case shutdowns. A blocked route alone is not proof if another path may exist.
9. Examine custom constraints, fixed totals, effective FX/LB/UB assignments and the propagation chain in a model certificate. Integer restrictions may be relaxed; semi-continuous zero remains allowed.
10. Correlate native failure rows with input property limits, recursion/separation coefficients and case switches. Failed iterates are not business allocations. Preserve intermediate/latest/profile labels.

## Reusable algebraic patterns

- Single required case: capacity lower bound `L` and association `a>0` imply `flow>=L/a`. If every available feed has property at least `P` and the required upper bound is `U<P`, then `U*flow-sum(property_i*feed_i)>=0` contradicts positive flow under that balance's actual assumptions.
- Utility supply: if every associated case consumes utility with `c_i/a_i>0`, capacity lower bound `L` implies demand at least `L*min(c_i/a_i)`. Prove supply is absent before calling it a conflict.
- Closed route and aggregate load: a positive custom total may imply a group must run, while one feed path is disabled. This is a candidate until substitute feed and route capacities are bounded.
- Property binding on success: require actual Q/XQ value, effective bounds, active flow and applicable source row. Fixed properties are separate. Unknown marginal is null; a genuine zero remains zero. Local marginals are not promises of profit after a change.

If evidence stops short, report what is known and what artifact would resolve it. Do not run an extra solver as part of these tools. A separately requested sensitivity study must use copies and record exact changes, solver versions and termination; never overwrite original input or change initialization silently.

When maintaining the playbook, save symptom, assumptions, algebraic proof or candidate evidence, excluded causes, alternative repairs and test coverage. Customer data stays local. Do not promote heuristic clues to proven rules based only on one successful relaxation.
