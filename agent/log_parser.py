"""
log_parser.py — Clean and deduplicate raw container logs before sending to LLM.

Pipeline:
  1. Strip Docker timestamp prefix from each line
  2. Remove empty lines
  3. Deduplicate consecutive repeated lines (keep first + count)
  4. Prioritize: ERROR/CRITICAL lines first, then WARNING, then the rest
  5. Truncate to max_lines to fit LLM context

This keeps the LLM input focused on what matters.
"""

import re


# Lines matching these patterns will be surfaced first
_PRIORITY_PATTERNS = [
    re.compile(r'\b(ERROR|CRITICAL|FATAL|EXCEPTION|TRACEBACK|panic)\b', re.IGNORECASE),
]
_WARNING_PATTERNS = [
    re.compile(r'\b(WARN|WARNING|DEPRECATED|SLOW)\b', re.IGNORECASE),
]

# Docker adds a timestamp prefix like "2026-03-28T08:00:00.123456789Z "
_TIMESTAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T[\d:\.]+Z\s+')


def _strip_timestamp(line: str) -> str:
    return _TIMESTAMP_RE.sub("", line)


def _deduplicate(lines: list[str]) -> list[str]:
    """Collapse consecutive identical lines into 'line (xN)'."""
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        count = 1
        while i + count < len(lines) and lines[i + count] == line:
            count += 1
        if count > 1:
            result.append(f"{line}  (x{count})")
        else:
            result.append(line)
        i += count
    return result


def parse(raw_logs: str, max_lines: int = 80) -> str:
    """
    Clean raw Docker log output and return a prioritized, deduplicated text block.

    Args:
        raw_logs:  raw string from container.logs()
        max_lines: max lines to return (to keep LLM input manageable)

    Returns:
        Cleaned log text ready to send to the LLM.
    """
    # Split and strip timestamps
    lines = [_strip_timestamp(l) for l in raw_logs.splitlines()]
    lines = [l.strip() for l in lines if l.strip()]

    if not lines:
        return "[No log content]"

    # Separate by priority
    errors   = [l for l in lines if any(p.search(l) for p in _PRIORITY_PATTERNS)]
    warnings = [l for l in lines if not any(p.search(l) for p in _PRIORITY_PATTERNS)
                                 and any(p.search(l) for p in _WARNING_PATTERNS)]
    rest     = [l for l in lines if not any(p.search(l) for p in _PRIORITY_PATTERNS)
                                 and not any(p.search(l) for p in _WARNING_PATTERNS)]

    # Deduplicate each group
    errors   = _deduplicate(errors)
    warnings = _deduplicate(warnings)
    rest     = _deduplicate(rest)

    # Build prioritized output
    output_lines = []

    if errors:
        output_lines.append("=== ERRORS / CRITICAL ===")
        output_lines.extend(errors)
        output_lines.append("")

    if warnings:
        output_lines.append("=== WARNINGS ===")
        output_lines.extend(warnings)
        output_lines.append("")

    if rest:
        output_lines.append("=== INFO / DEBUG ===")
        # Only keep last N lines of info to save context
        info_budget = max_lines - len(errors) - len(warnings)
        output_lines.extend(rest[-max(info_budget, 10):])

    return "\n".join(output_lines[:max_lines + 10])  # slight buffer for section headers
