"""Explicit lists of issue numbers: ``plan --issues`` and the manager's ``issues`` plan field.

One validator for both, so the command line and the HTTP API accept exactly the same
lists. Messages are fixed text: they never echo the offending value.
"""

import argparse

MAX_PLAN_ISSUES = 100
MAX_ISSUE_NUMBER = 2**31 - 1
_PROBLEM = f"must be 1 to {MAX_PLAN_ISSUES} issue numbers from 1 to {MAX_ISSUE_NUMBER}"


def check_issue_numbers(values):
    """The list as positive ints with duplicates removed (first occurrence wins); ValueError otherwise."""
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError(_PROBLEM)
    numbers = []
    for item in values:
        if type(item) is not int or not 1 <= item <= MAX_ISSUE_NUMBER:
            raise ValueError(_PROBLEM)
        if item not in numbers:
            numbers.append(item)
    if len(numbers) > MAX_PLAN_ISSUES:
        raise ValueError(_PROBLEM)
    return numbers


def parse_issue_numbers(text):
    """``"3,5,7"`` as ``[3, 5, 7]``: ASCII digits only, no empty item, no sign, no ``#``."""
    if not isinstance(text, str) or len(text) > 16 * (MAX_PLAN_ISSUES * 4):
        raise ValueError(_PROBLEM)
    numbers = []
    for part in text.split(","):
        part = part.strip()
        if not (part.isascii() and part.isdigit()) or len(part) > 10:
            raise ValueError(_PROBLEM)
        numbers.append(int(part))
    return check_issue_numbers(numbers)


def issues_argument(text):
    """The argparse ``type`` of ``--issues``."""
    try:
        return parse_issue_numbers(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def issues_flag(numbers):
    """The single argv item ``--issues=3,5,7`` (the value can never be read as an option)."""
    return "--issues=" + ",".join(str(number) for number in numbers)
