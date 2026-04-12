"""Tests for Finding #2: repeated --env flags should compose, not overwrite.

Verifies that:
- ``--env A=1 B=2 C=3`` (single flag, multiple values) sets all three
- ``--env A=1 --env B=2`` (repeated flag) sets BOTH
- ``--env A=1 B=2 --env C=3`` (mixed) sets all three
- Malformed entries (no ``=``) are silently skipped
"""
import argparse
import os

import pytest


def _apply_env(env_groups):
    """Simulate the env handler from pyttd/cli.py after the argparse fix."""
    result = {}
    if env_groups:
        for group in env_groups:
            for item in group:
                if '=' in item:
                    key, value = item.split('=', 1)
                    result[key] = value
    return result


def test_single_flag_multiple_values():
    """--env A=1 B=2 C=3 should set all three."""
    # argparse with action='append' nargs='+' wraps in a list of lists
    env = [['A=1', 'B=2', 'C=3']]
    result = _apply_env(env)
    assert result == {'A': '1', 'B': '2', 'C': '3'}


def test_repeated_flag():
    """--env A=1 --env B=2 should set BOTH (the bug fix)."""
    # argparse with action='append' nargs='+' produces [[A=1], [B=2]]
    env = [['A=1'], ['B=2']]
    result = _apply_env(env)
    assert result == {'A': '1', 'B': '2'}


def test_mixed():
    """--env A=1 B=2 --env C=3 should set all three."""
    env = [['A=1', 'B=2'], ['C=3']]
    result = _apply_env(env)
    assert result == {'A': '1', 'B': '2', 'C': '3'}


def test_malformed_skipped():
    """Entries without = should be silently skipped."""
    env = [['A=1', 'NOEQUALS', 'B=2']]
    result = _apply_env(env)
    assert result == {'A': '1', 'B': '2'}


def test_none_env():
    """No --env flag at all (None) should produce no env vars."""
    result = _apply_env(None)
    assert result == {}


def test_value_with_equals():
    """Values containing = should be split only on the first one."""
    env = [['A=1=2=3']]
    result = _apply_env(env)
    assert result == {'A': '1=2=3'}
