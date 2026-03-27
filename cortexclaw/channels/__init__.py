"""Auto-import all channel modules so they self-register."""

from . import cli  # noqa: F401

# Slack is imported conditionally — it requires slack_bolt
try:
    from . import slack  # noqa: F401
except ImportError:
    pass
