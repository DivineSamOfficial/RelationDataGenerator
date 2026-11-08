"""Exception hierarchy. Every error raised on purpose by scmgen derives from ScmgenError,
so the CLI can tell expected failures (bad config, unreachable database) from bugs."""


class ScmgenError(Exception):
    """Base class for all expected scmgen errors."""


class ConfigError(ScmgenError):
    """Configuration is missing, malformed or inconsistent."""


class DatabaseConnectionError(ScmgenError):
    """MySQL could not be reached or rejected the login."""
