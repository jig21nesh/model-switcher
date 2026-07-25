<!-- model-switcher:begin — managed by model-switcher install.sh; do not edit inside this block -->
## Model-switcher routing policy (MANDATORY)

This machine uses model-switcher: the session runs on a low-cost model, and prompts the router
classifies as more demanding must be executed by a subagent running a stronger model.

- When a user prompt carries a `[model-switcher] ... MANDATORY ROUTING POLICY` directive, treat it
  as policy, not a suggestion: your FIRST action is to spawn the subagent **named in that
  directive** with the user's full request and any context it needs, then relay its result.
- The directive names the exact agent to use — `heavy-task-*` for prompts classified COMPLEX, and
  `mid-task-*` for prompts classified MODERATE where a middle tier is configured. Always use the
  one the directive names; never substitute a different tier.
- Do not perform the task in-session unless the user's message explicitly says not to delegate.
- For prompts without a model-switcher directive, this policy does not apply — never mention it.
<!-- model-switcher:end -->
