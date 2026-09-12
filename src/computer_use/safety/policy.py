"""Explicit, deny-first policy gateway shared by discovery and replay."""

from fnmatch import fnmatchcase
from typing import Literal, Protocol
from urllib.parse import SplitResult, urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from computer_use.artifacts.models import RiskLevel, Target

ActionType = Literal["navigate", "click", "type", "select", "extract", "wait"]


def default_allowed_actions() -> set[ActionType]:
    return {"navigate", "click", "type", "select", "extract", "wait"}


def default_approval_risks() -> set[RiskLevel]:
    return {RiskLevel.IRREVERSIBLE}


class SafetyPolicy(BaseModel):
    """Request-scoped domain, route, action, risk, and approval envelope."""

    model_config = ConfigDict(extra="forbid")
    allowed_origins: list[AnyHttpUrl] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_routes: list[str] = Field(default_factory=lambda: ["*"])
    allowed_actions: set[ActionType] = Field(default_factory=default_allowed_actions)
    denied_actions: set[ActionType] = Field(default_factory=set)
    maximum_risk: RiskLevel = RiskLevel.READ_ONLY
    require_human_approval_for: set[RiskLevel] = Field(default_factory=default_approval_risks)
    allow_coordinate_targets: bool = False

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        return [value.lower().rstrip(".") for value in values]


class PolicyAction(BaseModel):
    """Concrete, fully bound operation checked immediately before execution."""

    model_config = ConfigDict(extra="forbid")
    action_type: ActionType
    action_id: str | None = None
    target: Target | None = None
    url: str | None = None
    value: str | None = None
    risk: RiskLevel = RiskLevel.READ_ONLY


class PolicyContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    current_url: str
    mode: str
    approved_action_ids: set[str] = Field(default_factory=set)


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed: bool
    reason: str
    requires_human_approval: bool = False


class PolicyEngine(Protocol):
    """Authorize every fully bound action before the surface sees it."""

    async def authorize(
        self,
        action: PolicyAction,
        context: PolicyContext,
        policy: SafetyPolicy,
    ) -> PolicyDecision: ...


class ExplicitPolicyEngine:
    """Deterministic deny-first policy evaluator with default-denied irreversible work."""

    async def authorize(
        self,
        action: PolicyAction,
        context: PolicyContext,
        policy: SafetyPolicy,
    ) -> PolicyDecision:
        if action.action_type in policy.denied_actions:
            return PolicyDecision(allowed=False, reason="action type is explicitly denied")
        if action.action_type not in policy.allowed_actions:
            return PolicyDecision(allowed=False, reason="action type is not allowlisted")

        current = urlsplit(context.current_url)
        if current.scheme in {"http", "https"}:
            denied = self._url_denial(current, policy)
            if denied:
                return PolicyDecision(allowed=False, reason=f"current URL {denied}")
        if action.url is not None:
            denied = self._url_denial(urlsplit(action.url), policy)
            if denied:
                return PolicyDecision(allowed=False, reason=f"navigation URL {denied}")

        if self._risk_rank(action.risk) > self._risk_rank(policy.maximum_risk):
            return PolicyDecision(allowed=False, reason="action exceeds maximum risk")

        if action.risk in policy.require_human_approval_for:
            approval_id = action.action_id
            if approval_id is None or approval_id not in context.approved_action_ids:
                return PolicyDecision(
                    allowed=False,
                    reason="action requires explicit human approval",
                    requires_human_approval=True,
                )

        if action.target is not None and not policy.allow_coordinate_targets:
            if any(candidate.strategy == "coordinates" for candidate in action.target.candidates):
                return PolicyDecision(allowed=False, reason="coordinate targets are disabled")
        return PolicyDecision(allowed=True, reason="operation satisfies the explicit policy")

    @staticmethod
    def _url_denial(url: SplitResult, policy: SafetyPolicy) -> str | None:
        allowed_origins = {_origin(urlsplit(str(origin))) for origin in policy.allowed_origins}
        hostname = url.hostname
        domain_allowed = hostname is not None and hostname.lower() in policy.allowed_domains
        if _origin(url) not in allowed_origins and not domain_allowed:
            return "domain is not allowlisted"
        route = url.path or "/"
        if not any(fnmatchcase(route, pattern) for pattern in policy.allowed_routes):
            return "route is not allowlisted"
        return None

    @staticmethod
    def _risk_rank(risk: RiskLevel) -> int:
        return {
            RiskLevel.READ_ONLY: 0,
            RiskLevel.REVERSIBLE_WRITE: 1,
            RiskLevel.IRREVERSIBLE: 2,
        }[risk]


def classify_action(action_type: ActionType, *, final_submission: bool = False) -> RiskLevel:
    """Conservative default classification; artifacts may declare a stricter class."""

    if final_submission:
        return RiskLevel.IRREVERSIBLE
    if action_type in {"type", "select"}:
        return RiskLevel.REVERSIBLE_WRITE
    return RiskLevel.READ_ONLY


def _origin(url: SplitResult) -> tuple[str, str | None, int | None]:
    default_port = 443 if url.scheme == "https" else 80 if url.scheme == "http" else None
    return (url.scheme, url.hostname, url.port or default_port)


# Compatibility name retained for existing callers.
AllowlistPolicyEngine = ExplicitPolicyEngine
