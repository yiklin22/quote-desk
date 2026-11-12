"""Limits and caps. Each can be set with an environment variable of the
same name prefixed QUOTEDESK_."""
import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(f"QUOTEDESK_{name}", default))


# Input limits
MAX_ENQUIRY_CHARS = _int("MAX_ENQUIRY_CHARS", 20_000)           # one pasted enquiry
MAX_PRICE_LIST_BYTES = _int("MAX_PRICE_LIST_BYTES", 100_000)    # one published price list
MAX_REQUEST_BYTES = _int("MAX_REQUEST_BYTES", 500_000)          # one HTTP request, checked before the body is read
MAX_CUSTOM_LINES = _int("MAX_CUSTOM_LINES", 50)                 # extra lines on one quote

# Usage caps, per account
MAX_ENQUIRIES = _int("MAX_ENQUIRIES", 500)                      # enquiries stored at one time
MAX_ENQUIRIES_PER_DAY = _int("MAX_ENQUIRIES_PER_DAY", 100)      # new enquiries read per UTC day
MAX_PRICE_LISTS_PER_DAY = _int("MAX_PRICE_LISTS_PER_DAY", 20)   # price list versions published per UTC day

# Model reading of enquiries. Off unless QUOTEDESK_MODEL_EXTRACTION=1; needs ANTHROPIC_API_KEY in the
# server's environment. When on, the enquiry text is sent to the Anthropic API.
MODEL_EXTRACTION = os.environ.get("QUOTEDESK_MODEL_EXTRACTION", "") == "1"
MODEL = os.environ.get("QUOTEDESK_MODEL", "claude-opus-5-5")
MODEL_EFFORT = os.environ.get("QUOTEDESK_MODEL_EFFORT", "low")
MODEL_TIMEOUT_SECONDS = _int("MODEL_TIMEOUT_SECONDS", 30)
MAX_MODEL_CALLS_PER_DAY = _int("MAX_MODEL_CALLS_PER_DAY", 50)   # per account, per UTC day

# Accounts
MAX_ACCOUNTS = _int("MAX_ACCOUNTS", 25)
INVITE_CODE = os.environ.get("QUOTEDESK_INVITE_CODE", "")       # when set, signing up needs it
SESSION_DAYS = _int("SESSION_DAYS", 14)
COOKIE_SECURE = os.environ.get("QUOTEDESK_COOKIE_SECURE", "") == "1"   # https requests always get a Secure cookie
MIN_PASSWORD_CHARS = 10
LOGIN_WINDOW_MINUTES = 15
MAX_LOGIN_FAILURES_PER_EMAIL = 5
MAX_LOGIN_FAILURES_PER_IP = 30
