<!-- model-switcher:begin — managed by model-switcher install.sh; do not edit inside this block -->
## Model-switcher routing policy (MANDATORY)

This machine uses model-switcher: the session runs on a low-cost model and complex prompts must be
executed by the `heavy-task` subagent, which runs on a stronger model.

- When a user prompt carries a `[model-switcher] ... classified COMPLEX` directive, treat it as
  mandatory routing policy, not a suggestion: your FIRST action is to spawn the `heavy-task`
  subagent with the user's full request and any context it needs, then relay its result.
- Do not perform the complex task in-session unless the user's message explicitly says not to
  delegate.
- For prompts without a model-switcher directive, this policy does not apply — never mention it.
<!-- model-switcher:end -->
