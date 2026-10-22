# 0024 — `choose_group`: an interactive picker on MCP 2026-07-28 (requires FastMCP 4)

**Status:** Decided (2026-10).

## Context

`list_groups` returns every allowlisted group matching a fuzzy name substring, and its docstring asks
the model to "ask the user before falling back to a looser match". That is a convention, not an
enforced step, and a substring match happily returns unrelated groups that share a word.

FastMCP 4 (on the sessionless MCP 2026-07-28 protocol) lets a tool return an `InputRequiredResult`
describing a question; the client asks the user and calls the tool again with the answer. The
repo already ran FastMCP 4.1.3 (`uv.lock`); the Claude client in use speaks 2026-07-28 (stated by
the maintainer; not independently verified here).

## Decision

Add `choose_group(query, tags)`:

- Matching is the shared `_find_groups` helper (extracted from `list_groups`, same allowlist and
  community rules, same tag/query semantics) so the two tools can't disagree.
- One match → returned directly. Several (up to `MAX_GROUP_CHOICES` = 20) → a titled single-choice
  picker with a "None of these" option. More → `too_many` and a request to narrow the search.
- **Stateless.** Round two re-runs the search and checks the user's pick against the *current* matches;
  nothing is carried in `request_state`, and a pick that isn't among them (hidden group, a group that
  vanished, a forged jid) resolves to `stale`. Declining or "none of these" is never guessed past.
- Picker labels strip control characters and are length-bounded (group names are third-party text).
- `fastmcp` floor raised from `>=3.0.2` to `>=4.0`: `InputRequiredResult` doesn't exist before it.

## Consequences / not verified

- **No fallback for older clients.** On a handshake-era connection FastMCP rejects the call with an
  error naming the era mismatch; `list_groups` remains for those. A graceful fallback would need to
  detect the negotiated era, and the only helper for that (`Context._is_modern_protocol`) is private.
- **Single Machine:** the automatic per-process request-state key is enough because we carry no
  state; a multi-replica deployment would only need `REQUEST_STATE_KEY` if state is added later.
- Verified with a real in-memory client↔server round trip on 2026-07-28 (ask, answer, resolve;
  decline; legacy error). **Not verified:** how the Claude client renders the picker, or that the
  connector actually negotiates 2026-07-28 against the deployed server.
- Left alone: the `ctx.elicit()` confirmations in the permanently blocked outbound tools raise on
  modern connections (FastMCP 4 era-gates `ctx.elicit`). Unreachable today; would need the same
  multi-round pattern if a tool were ever unblocked.

## Update: first live result (2026-10-22)

On the deployed server the **Claude iOS app** loaded the tool and called it, then answered the picker
with `cancel` on its own — no picker is shown on that surface. Two things followed:

- An empty `input_responses` map is not an answer (FastMCP documents it as `None` *or* "retried without
  responses"), so a request counts as an answer only if it carries the `group` key (#42), and the result
  states which action the client replied with.
- **`choose_group` degrades to chat instead of failing.** On `cancel` (dismissed, or a client that can't
  render the picker) the result carries `candidates`, and the model puts the same choice to the user in
  chat; an explicit `decline` returns no candidates and is not re-asked. This keeps one tool that is
  interactive where supported, with no need to detect the client (the only public signal for that is
  absent). Not yet checked: whether claude.ai web or the desktop app render the picker.
