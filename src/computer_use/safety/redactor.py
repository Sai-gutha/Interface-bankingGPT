"""Recursive sensitive-data redaction for logs and persisted capability payloads."""

import re
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, SecretStr

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "ssn",
}
CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+"),
)


class SensitiveDataError(ValueError):
    """A payload intended for persistence still contains a known secret."""


class Redactor:
    """Replace configured secrets, credential patterns, and sensitive-key values recursively."""

    def __init__(self, secrets: Iterable[str | SecretStr] = ()) -> None:
        values = (
            item.get_secret_value() if isinstance(item, SecretStr) else item for item in secrets
        )
        self._secrets = tuple(sorted({item for item in values if item}, key=len, reverse=True))

    def redact(self, value: object, *, key: str | None = None) -> object:
        if key is not None and key.lower() in SENSITIVE_KEYS:
            return REDACTED
        if isinstance(value, SecretStr):
            return REDACTED
        if isinstance(value, BaseModel):
            return self.redact(value.model_dump(mode="json"))
        if isinstance(value, Mapping):
            return {str(k): self.redact(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    def assert_safe(self, value: object) -> None:
        serialized = str(value)
        if any(secret in serialized for secret in self._secrets):
            raise SensitiveDataError("payload contains a configured secret")
        if self._redact_text(serialized) != serialized:
            raise SensitiveDataError("payload contains credential-like data")

    def _redact_text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        for pattern in CREDENTIAL_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
