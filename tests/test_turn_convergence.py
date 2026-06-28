"""Unit tests for the per-turn convergence backstop (ROUND_CAP_RESILIENCE_PLAN.md).

Covers #2 (soft-budget wind-down) and #3 (one-PR-boundary) — the pure decision
helpers, no LLM/API calls.
"""

from app.turn_convergence import (
    convergence_message,
    should_converge,
    soft_budget,
)


class TestSoftBudget:
    def test_default_30_at_075(self):
        # ceil(30 * 0.75) = 23, and 23 <= 29, so fires on round 23.
        assert soft_budget(30, 0.75) == 23

    def test_clamped_below_hard_cap(self):
        # fraction 1.0 must still leave a round to converge -> max_rounds - 1.
        assert soft_budget(30, 1.0) == 29

    def test_never_zero(self):
        assert soft_budget(10, 0.0) == 1

    def test_degenerate_cap(self):
        assert soft_budget(1, 0.75) == 1
        assert soft_budget(0, 0.75) == 1

    def test_small_cap_for_uat(self):
        # UAT sets CHAT_MAX_TOOL_ROUNDS=6 -> ceil(4.5)=5, one before the cap.
        assert soft_budget(6, 0.75) == 5


class TestShouldConverge:
    def test_below_soft_budget_no_nudge(self):
        assert should_converge(5, 30, [], soft_fraction=0.75) is None

    def test_at_soft_budget_fires(self):
        assert (
            should_converge(23, 30, [], soft_fraction=0.75) == "soft_budget"
        )

    def test_past_soft_budget_fires(self):
        assert (
            should_converge(28, 30, [], soft_fraction=0.75) == "soft_budget"
        )

    def test_pr_open_is_a_boundary_even_early(self):
        trace = [{"name": "open_pr", "args": {}, "result": "ok"}]
        assert (
            should_converge(2, 30, trace, soft_fraction=0.75) == "pr_boundary"
        )

    def test_pr_merge_is_a_boundary(self):
        trace = [{"name": "merge_pr", "args": {}, "result": "ok"}]
        assert (
            should_converge(3, 30, trace, soft_fraction=0.75) == "pr_boundary"
        )

    def test_open_fix_pr_is_a_boundary(self):
        trace = [{"name": "open_fix_pr", "args": {}, "result": "ok"}]
        assert (
            should_converge(1, 30, trace, soft_fraction=0.75) == "pr_boundary"
        )

    def test_pr_boundary_takes_precedence_over_soft(self):
        trace = [{"name": "merge_pr", "args": {}, "result": "ok"}]
        assert (
            should_converge(28, 30, trace, soft_fraction=0.75) == "pr_boundary"
        )

    def test_non_pr_tools_do_not_trigger_boundary(self):
        trace = [
            {"name": "read_repo_file", "args": {}, "result": "..."},
            {"name": "ssh_run", "args": {}, "result": "..."},
        ]
        assert should_converge(5, 30, trace, soft_fraction=0.75) is None

    def test_none_trace_is_safe(self):
        assert should_converge(5, 30, None, soft_fraction=0.75) is None


class TestConvergenceMessage:
    def test_soft_budget_message_is_user_role_and_resumable(self):
        msg = convergence_message("soft_budget", 23, 30)
        assert msg["role"] == "user"
        assert "RESUME HERE" in msg["content"]
        assert "23 of 30" in msg["content"]

    def test_pr_boundary_message_says_stop_one_pr(self):
        msg = convergence_message("pr_boundary", 4, 30)
        assert msg["role"] == "user"
        assert "RESUME HERE" in msg["content"]
        assert "one-PR-per-turn" in msg["content"]

    def test_unknown_reason_defaults_to_soft_copy(self):
        # Defensive: an unexpected reason should still yield a wind-down message.
        msg = convergence_message("something_else", 10, 30)
        assert msg["role"] == "user"
        assert "RESUME HERE" in msg["content"]
