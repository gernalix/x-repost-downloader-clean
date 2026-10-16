class XRepostError(RuntimeError):
    """Base application error."""


class AuthenticationRequired(XRepostError):
    """The authenticated X session is missing or expired."""


class BrowserProfileLocked(AuthenticationRequired):
    """The browser cookie database cannot be copied while the browser is open."""


class RateLimited(XRepostError):
    """X is currently rate limiting the session."""


class CheckpointRequired(XRepostError):
    """X requires manual account verification or a CAPTCHA."""


class BrowserUnavailable(XRepostError):
    """The configured browser cannot be launched."""


class LockBusy(XRepostError):
    """Another application instance owns the run lock."""
