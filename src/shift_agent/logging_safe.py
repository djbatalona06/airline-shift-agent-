"""Keep secrets out of log output.

Exception text from a portal is untrusted for logging purposes. A failing
request can easily carry a session token or a password in its message, and that
text otherwise lands verbatim in a log file — which rotates to disk, gets copied
into a support thread, or ends up in a screenshot.

Two layers, deliberately:

* `scrub()` at known-risky call sites, where third-party text enters a log call.
  This is what makes the behaviour testable, since it happens before the record
  is created.
* `install()` adds a filter to the handlers as a backstop for call sites nobody
  remembered to scrub.
"""

from __future__ import annotations

import logging
import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Labelled secrets: password=…, token: …, api_key=…
    (re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)\b(token|session|sessionid|api[_-]?key|authorization|auth)\b\s*[=:]\s*\S+"),
     r"\1=[REDACTED]"),
    # Unlabelled but unmistakable.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[JWT]"),
    (re.compile(r"\b[A-Fa-f0-9]{24,}\b"), "[HEX-TOKEN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
)


def scrub(value: object) -> str:
    text = str(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SecretScrubbingFilter(logging.Filter):
    """Scrub a record's message and arguments in place.

    Returns True always — the record is cleaned, never dropped. Losing the fact
    that an error happened would be worse than the leak this prevents.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: scrub(v) for k, v in record.args.items()}
            else:
                record.args = tuple(scrub(a) for a in record.args)
        return True


def install(logger: logging.Logger | None = None) -> None:
    """Attach the filter to every handler on `logger` (root by default).

    Handlers rather than loggers: a filter on a logger only sees records logged
    directly to it, not to its children, so `shift_agent.poller` would slip past
    a filter installed on `shift_agent`.
    """
    target = logger or logging.getLogger()
    scrubber = SecretScrubbingFilter()
    for handler in target.handlers:
        if not any(isinstance(f, SecretScrubbingFilter) for f in handler.filters):
            handler.addFilter(scrubber)
