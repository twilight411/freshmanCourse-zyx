"""Separate model SQL failures from execution-harness failures."""

import re


def execution_issue(error):
    """Return a reason when an execution failure cannot establish SQL correctness."""
    value = str(error or "")
    if not value:
        return None
    if re.search(r"\b3024\b|maximum statement execution time|Timeout exceeded|timed out", value, re.I):
        return "execution_timeout"
    if re.search(r"\b(?:2003|2006|2013|2055)\b|Lost connection|server has gone away|connection refused", value, re.I):
        return "database_connection"
    if "Result exceeds" in value and "rows" in value:
        return "result_limit"
    return None
