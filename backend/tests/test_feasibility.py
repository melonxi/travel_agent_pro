"""Tests for harness.feasibility module."""
from __future__ import annotations
import pytest
from harness.feasibility import check_feasibility, FeasibilityResult

class TestCheckFeasibility:
    def test_no_destination_returns_feasible(self):
        r = check_feasibility(None, 1000, 3)
        assert r.feasible is True
        assert r.reasons == []

    def test_sufficient_budget_and_days(self):
        r = check_feasibility("东京", 10000, 5)
        assert r.feasible is True

    def test_insufficient_days(self):
        r = check_feasibility("东京", 10000, 1)
        assert r.feasible is False
        assert any("至少3天" in reason for reason in r.reasons)

    def test_insufficient_daily_budget(self):
        r = check_feasibility("东京", 500, 5)
        assert r.feasible is False
        assert any("日均预算" in reason for reason in r.reasons)

    def test_insufficient_total_budget_no_days(self):
        r = check_feasibility("巴黎", 500, None)
        assert r.feasible is False
        assert any("总预算" in reason for reason in r.reasons)

    def test_unknown_destination_uses_defaults(self):
        r = check_feasibility("火星", 100, 1)
        assert r.feasible is False

    def test_budget_none_days_ok(self):
        r = check_feasibility("东京", None, 5)
        assert r.feasible is True

    def test_both_none(self):
        r = check_feasibility("东京", None, None)
        assert r.feasible is True
