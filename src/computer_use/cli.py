"""Runnable vertical-slice commands used by the submission demo."""

import argparse
import asyncio
import json
import shutil
from pathlib import Path

import uvicorn
from pydantic import AnyHttpUrl, BaseModel, SecretStr

from computer_use.api.app import create_app
from computer_use.artifacts.compiler import ArtifactCompiler, CompilerSpec, RuntimeBinding
from computer_use.artifacts.models import (
    BusinessOutcome,
    CapabilityArtifact,
    InputParameter,
    OutputParameter,
    ReuseMetadata,
    RiskLevel,
    TargetApplication,
    TextCondition,
    ValueType,
)
from computer_use.config import Settings
from computer_use.discovery.engine import DiscoveryEngine, DiscoveryRequest
from computer_use.discovery.openai_provider import OpenAIDecisionProvider
from computer_use.handoff import InMemoryHandoffCoordinator
from computer_use.observability.events import JsonlEventSink
from computer_use.observability.evidence import FileEvidenceStore
from computer_use.replay.engine import ReplayEngine, ReplayRequest, ReplayResult
from computer_use.safety.policy import ExplicitPolicyEngine, SafetyPolicy
from computer_use.safety.redactor import REDACTED, Redactor
from computer_use.surfaces.playwright import PlaywrightSurfaceAdapter

DEFAULT_ARTIFACT = Path("artifacts/examples/lookup_savings_balance.v1.json")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Computer-use automation vertical slice")
    commands = root.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="Run genuine LLM discovery and compile it")
    discover.add_argument("--goal", required=True)
    discover.add_argument("--member-id", required=True)
    discover.add_argument("--start-url", default="http://127.0.0.1:8001/")
    discover.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT)
    discover.add_argument("--headed", action="store_true")

    for name, help_text, default_member in (
        ("replay", "Replay an artifact without an LLM", "12345"),
        ("failure-demo", "Replay the member-not-found business outcome", "55555"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
        command.add_argument("--member-id", default=default_member)
        command.add_argument("--headed", action="store_true")

    handoff = commands.add_parser("handoff-demo", help="Pause replay for live operator takeover")
    handoff.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    handoff.add_argument("--member-id", default="12345")
    handoff.add_argument("--operator-port", type=int, default=8000)
    handoff.add_argument("--headed", action="store_true")
    return root


async def discover(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.llm_model or settings.openai_api_key is None:
        raise RuntimeError(
            "OPENAI_API_KEY and COMPUTER_USE_LLM_MODEL are required for genuine discovery"
        )
    redactor = Redactor([args.member_id])
    events = JsonlEventSink(settings.evidence_dir, redactor)
    evidence = FileEvidenceStore(settings.evidence_dir, redactor)
    surface = await PlaywrightSurfaceAdapter.launch(headless=not args.headed)
    try:
        result = await DiscoveryEngine(
            surface,
            OpenAIDecisionProvider(settings.llm_model, api_key=settings.openai_api_key),
            ExplicitPolicyEngine(),
            events,
            evidence,
        ).discover(
            DiscoveryRequest(
                goal=args.goal,
                starting_url=args.start_url,
                safety_policy=SafetyPolicy(
                    allowed_origins=[args.start_url],
                    maximum_risk=RiskLevel.REVERSIBLE_WRITE,
                ),
            )
        )
        if result.status != "success":
            _write_json(settings.evidence_dir / result.run_id / "result.json", result, redactor)
            print(result.model_dump_json(indent=2))
            return 1
        artifact = ArtifactCompiler().compile(
            result, _compiler_spec(args.member_id, args.start_url)
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")
        run_dir = settings.evidence_dir / result.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "artifact.json").write_text(
            artifact.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        _write_json(run_dir / "result.json", result, redactor)
        _publish(run_dir, settings.evidence_dir / "submission" / "discovery")
        print(f"Discovery succeeded: {result.run_id}\nArtifact: {args.output}")
        return 0
    finally:
        await surface.close()


async def replay(args: argparse.Namespace, settings: Settings) -> int:
    artifact = _load_artifact(args.artifact)
    result = await _replay_once(artifact, args.member_id, settings, not args.headed)
    label = "replay-success" if args.command == "replay" else "business-outcome"
    _publish(
        settings.evidence_dir / result.run_id,
        settings.evidence_dir / "submission" / label,
    )
    submission_artifact = settings.evidence_dir / "submission" / "example-artifact.json"
    submission_artifact.parent.mkdir(parents=True, exist_ok=True)
    submission_artifact.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(result.model_dump_json(indent=2))
    return 0 if result.status.value in {"SUCCESS", "RECOVERED_SUCCESS", "BUSINESS_OUTCOME"} else 1


async def _replay_once(
    artifact: CapabilityArtifact, member_id: str, settings: Settings, headless: bool
) -> ReplayResult:
    redactor = Redactor([member_id])
    events = JsonlEventSink(settings.evidence_dir, redactor)
    surface = await PlaywrightSurfaceAdapter.launch(headless=headless)
    try:
        result = await ReplayEngine(
            surface,
            ExplicitPolicyEngine(),
            FileEvidenceStore(settings.evidence_dir, redactor),
            events,
        ).replay(ReplayRequest(artifact=artifact, parameters={"member_id": member_id}))
        _write_json(settings.evidence_dir / result.run_id / "result.json", result, redactor)
        return result
    finally:
        await surface.close()


async def handoff_demo(args: argparse.Namespace, settings: Settings) -> int:
    artifact = _load_artifact(args.artifact)
    artifact.steps[0].risk = RiskLevel.IRREVERSIBLE
    artifact.risk = RiskLevel.IRREVERSIBLE
    redactor = Redactor([args.member_id])
    events = JsonlEventSink(settings.evidence_dir, redactor)
    coordinator = InMemoryHandoffCoordinator(events)
    surface = await PlaywrightSurfaceAdapter.launch(headless=not args.headed)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(coordinator), host="127.0.0.1", port=args.operator_port, log_level="warning"
        )
    )
    server_task = asyncio.create_task(server.serve())
    replay_task = asyncio.create_task(
        ReplayEngine(
            surface,
            ExplicitPolicyEngine(),
            FileEvidenceStore(settings.evidence_dir, redactor),
            events,
            coordinator,
        ).replay(ReplayRequest(artifact=artifact, parameters={"member_id": args.member_id}))
    )
    try:
        while not await coordinator.list_active():
            if replay_task.done():
                break
            await asyncio.sleep(0.05)
        print(f"Operator takeover: http://127.0.0.1:{args.operator_port}/operator")
        result = await replay_task
        _write_json(settings.evidence_dir / result.run_id / "result.json", result, redactor)
        _publish(
            settings.evidence_dir / result.run_id,
            settings.evidence_dir / "submission" / "human-handoff",
        )
        print(result.model_dump_json(indent=2))
        return 0 if result.status.value in {"SUCCESS", "RECOVERED_SUCCESS"} else 1
    finally:
        server.should_exit = True
        await server_task
        await surface.close()


def _compiler_spec(member_id: str, start_url: str) -> CompilerSpec:
    return CompilerSpec(
        capability_id="lookup_savings_balance",
        name="Look up a member savings balance",
        capability_version="1.0.0",
        description="Look up a synthetic member and return the current savings balance.",
        target_application=TargetApplication(
            vendor_id="interface_ai_demo",
            product_id="banking_operations",
            display_name="AI-interface Banking Operations",
            surface_kind="legacy_web",
            entry_point=start_url,
            allowed_origins=[AnyHttpUrl(start_url)],
        ),
        bindings=[
            RuntimeBinding(
                parameter=InputParameter(
                    name="member_id",
                    value_type=ValueType.STRING,
                    description="Five-digit synthetic member identifier",
                    sensitive=True,
                ),
                runtime_value=SecretStr(member_id),
            )
        ],
        outputs=[
            OutputParameter(
                name="savings_balance",
                value_type=ValueType.NUMBER,
                description="Current savings balance",
                sensitive=True,
            )
        ],
        business_outcomes=[
            BusinessOutcome(
                code="MEMBER_NOT_FOUND",
                description="No member matched the supplied identifier.",
                when=[TextCondition(expected="Record not found")],
                output_values={"savings_balance": None},
            )
        ],
        reuse=ReuseMetadata(vendor_capability_key="banking.lookup_savings_balance"),
    )


def _load_artifact(path: Path) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def _write_json(path: Path, model: BaseModel, redactor: Redactor) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = redactor.redact(model.model_dump(mode="json"))
    if isinstance(safe, dict) and isinstance(safe.get("outputs"), dict):
        safe["outputs"] = {key: REDACTED for key in safe["outputs"]}
    path.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _publish(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


async def async_main() -> int:
    args = parser().parse_args()
    settings = Settings()
    if args.command == "discover":
        return await discover(args, settings)
    if args.command in {"replay", "failure-demo"}:
        return await replay(args, settings)
    if args.command == "handoff-demo":
        return await handoff_demo(args, settings)
    raise AssertionError(args.command)


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
