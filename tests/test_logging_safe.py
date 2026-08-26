"""SecretScrubbingFilter must scrub secret-shaped *text*, not break logging.

Found while wiring `main()`'s zero-argument path (setup/, tested in
test_main.py): `%d`/`%f`-style log calls with a non-string argument -
`log.exception("poll cycle failed (%d consecutive)", self.consecutive_failures)`
in poller.py, for one - broke as soon as install() had attached the filter,
because force-stringifying every arg turned that int into "3" and
`"%d" % ("3",)` raises TypeError. That's the failure this file pins.
"""

from __future__ import annotations

import logging

from shift_agent.logging_safe import SecretScrubbingFilter


def _filtered_record(msg: str, args: tuple) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )
    SecretScrubbingFilter().filter(record)
    return record


def test_string_args_are_scrubbed():
    record = _filtered_record("login failed: %s", ("password=hunter2",))
    assert record.getMessage() == "login failed: password=[REDACTED]"


def test_non_string_args_pass_through_unscrubbed_and_still_format():
    record = _filtered_record("poll cycle failed (%d consecutive)", (3,))
    assert record.getMessage() == "poll cycle failed (3 consecutive)"


def test_mixed_string_and_non_string_args():
    record = _filtered_record("%s failed %d times: %s", ("cycle", 3, "token=abc123"))
    assert record.getMessage() == "cycle failed 3 times: token=[REDACTED]"


def test_dict_args_scrub_only_string_values():
    record = _filtered_record("%(name)s retried %(count)d times", {"name": "cycle", "count": 3})
    assert record.getMessage() == "cycle retried 3 times"
