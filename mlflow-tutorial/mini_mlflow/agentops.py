"""Agent operations on top of traces: sessions, simulation, scoring and triage.

MLflow 3.10 introduced conversation-level evaluation, and 3.18 added automatic
issue detection, online code scorers and statistical comparison of evaluation
runs.  This module implements the smallest *runnable* core of those ideas on
top of MiniMLflow's existing ``Trace`` and ``Assessment`` primitives.

Design choices that matter:

* a **session** is derived from the trace tag ``mlflow.trace.session``; there
  is no new table, because the trace is already the durable record;
* simulation is **scripted** rather than LLM-driven: a test case carries the
  user turns explicitly, so runs are deterministic and free; real MLflow uses
  a model to role-play the user;
* scorers are deterministic heuristics that return a scalar plus a rationale.
  They are labelled ``CODE`` so they can never be confused with human feedback
  or an LLM judge;
* online scoring samples at the **session** level and is idempotent because
  ``log_feedback`` upserts on ``(trace, name, source, expectation)``;
* issue detection groups evidence by a normalised signature instead of asking
  a model; comparison uses a paired bootstrap, not a two-sample test.

Production gaps are listed in chapter 46: LLM user simulation and goal
checking, asynchronous scoring, clustering-based issue detection, BCa
confidence intervals, multiple-comparison correction and retention policies.
"""

from __future__ import annotations

import hashlib
import random
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .entities import Assessment, Span, Trace
from .errors import InvalidParameter
from .utils import compact_json, new_id, now_ms

SESSION_TAG = "mlflow.trace.session"

_TOOL_SPAN_TYPES = {"TOOL", "TOOL_CALL", "FUNCTION"}
_TOKEN_INPUT_KEYS = (
    "usage.input_tokens",
    "input_tokens",
    "prompt_tokens",
    "gen_ai.usage.input_tokens",
)
_TOKEN_OUTPUT_KEYS = (
    "usage.output_tokens",
    "output_tokens",
    "completion_tokens",
    "gen_ai.usage.output_tokens",
)
_ERROR_EVENT_NAMES = {"exception", "error"}
_WHITESPACE = re.compile(r"\s+")
_HEXISH = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_SAFE_MESSAGE_KEYS = (
    "message",
    "question",
    "query",
    "input",
    "content",
    "prompt",
    "text",
    "answer",
)


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _message_text(value: Any, *, role: str | None = None) -> str:
    """Render a trace request/response as text without assuming one schema."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        messages = value.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            parts: list[str] = []
            for item in messages:
                if not isinstance(item, Mapping):
                    continue
                if role is not None and str(item.get("role", "")).lower() != role:
                    continue
                parts.append(_message_text(item.get("content")))
            return "\n".join(part for part in parts if part)
        for key in _SAFE_MESSAGE_KEYS:
            if key in value:
                return _message_text(value[key])
        return compact_json(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "\n".join(
            part for part in (_message_text(item) for item in value) if part
        )
    return str(value)


def _trace_user_message(trace: Trace) -> str:
    request = trace.request
    if isinstance(request, Mapping) and isinstance(request.get("messages"), Sequence):
        rendered = _message_text(request, role="user")
        if rendered:
            return rendered
    return _message_text(request)


def _trace_assistant_message(trace: Trace) -> str:
    return _message_text(trace.response)


def _span_tokens(span: Span) -> tuple[int, int]:
    """Read token usage once, preferring span attributes over events.

    Many instrumentations write both ``usage.input_tokens`` attributes and a
    ``token_usage`` event.  Summing both would double count; events are only a
    fallback for whichever side the attributes did not provide.
    """
    attributes = span.attributes if isinstance(span.attributes, Mapping) else {}
    attr_inputs = sum(_as_int(attributes.get(key)) for key in _TOKEN_INPUT_KEYS)
    attr_outputs = sum(_as_int(attributes.get(key)) for key in _TOKEN_OUTPUT_KEYS)
    event_inputs = 0
    event_outputs = 0
    for event in span.events or []:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("name", "")).lower() not in {"token_usage", "usage", "llm_usage"}:
            continue
        event_attributes = event.get("attributes")
        if not isinstance(event_attributes, Mapping):
            continue
        event_inputs += _as_int(event_attributes.get("input"))
        event_inputs += _as_int(event_attributes.get("input_tokens"))
        event_outputs += _as_int(event_attributes.get("output"))
        event_outputs += _as_int(event_attributes.get("output_tokens"))
    return (
        attr_inputs if attr_inputs else event_inputs,
        attr_outputs if attr_outputs else event_outputs,
    )


def _is_tool_span(span: Span) -> bool:
    return str(span.span_type or "").upper() in _TOOL_SPAN_TYPES


def _normalise_signature(text: Any, *, limit: int = 160) -> str:
    rendered = _QUOTED.sub("<str>", str(text or ""))
    rendered = _HEXISH.sub("<id>", rendered)
    rendered = _NUMBER.sub("<n>", rendered)
    rendered = _WHITESPACE.sub(" ", rendered).strip().lower()
    return rendered[:limit] or "<unknown>"


def _exception_from_span(span: Span) -> tuple[str, str]:
    for event in span.events or []:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("name", "")).lower() not in _ERROR_EVENT_NAMES:
            continue
        attributes = event.get("attributes")
        if not isinstance(attributes, Mapping):
            continue
        error_type = str(attributes.get("exception.type") or attributes.get("type") or "")
        message = str(
            attributes.get("exception.message") or attributes.get("message") or ""
        )
        if error_type or message:
            return error_type or "Error", message
    return "Error", str(span.outputs or "")


@dataclass
class Session:
    """A group of traces that belong to one conversation."""

    session_id: str
    traces: list[Trace]

    def __post_init__(self) -> None:
        if not self.session_id:
            raise InvalidParameter("session_id must not be empty")
        if not self.traces:
            raise InvalidParameter("a session must contain at least one trace")
        self.traces = sorted(self.traces, key=lambda trace: (trace.timestamp, trace.trace_id))

    @property
    def experiment_id(self) -> str:
        return self.traces[0].experiment_id

    @property
    def turn_count(self) -> int:
        return len(self.traces)

    @property
    def status(self) -> str:
        return "ERROR" if any(trace.status == "ERROR" for trace in self.traces) else "OK"

    @property
    def started_at(self) -> int:
        return min(trace.timestamp for trace in self.traces)

    @property
    def ended_at(self) -> int:
        return max(trace.timestamp for trace in self.traces)

    @property
    def latency_ms(self) -> float:
        starts: list[int] = []
        ends: list[int] = []
        for trace in self.traces:
            for span in trace.spans:
                starts.append(span.start_time_ns)
                if span.end_time_ns is not None:
                    ends.append(span.end_time_ns)
        if not starts or not ends:
            return float(max(0, self.ended_at - self.started_at))
        return max(0.0, (max(ends) - min(starts)) / 1_000_000)

    @property
    def error_count(self) -> int:
        return sum(1 for trace in self.traces for span in trace.spans if span.status == "ERROR")

    @property
    def failed_turns(self) -> int:
        return sum(1 for trace in self.traces if trace.status == "ERROR")

    @property
    def tool_calls(self) -> int:
        return sum(
            1 for trace in self.traces for span in trace.spans if _is_tool_span(span)
        )

    @property
    def tool_failures(self) -> int:
        return sum(
            1
            for trace in self.traces
            for span in trace.spans
            if _is_tool_span(span) and span.status == "ERROR"
        )

    @property
    def input_tokens(self) -> int:
        return sum(_span_tokens(span)[0] for trace in self.traces for span in trace.spans)

    @property
    def output_tokens(self) -> int:
        return sum(_span_tokens(span)[1] for trace in self.traces for span in trace.spans)

    @property
    def user_messages(self) -> tuple[str, ...]:
        return tuple(_trace_user_message(trace) for trace in self.traces)

    @property
    def assistant_messages(self) -> tuple[str, ...]:
        return tuple(_trace_assistant_message(trace) for trace in self.traces)

    @property
    def final_response(self) -> str:
        for trace in reversed(self.traces):
            rendered = _trace_assistant_message(trace)
            if rendered:
                return rendered
        return ""

    def transcript(self) -> list[dict[str, str]]:
        turns: list[dict[str, str]] = []
        for trace in self.traces:
            turns.append({"role": "user", "content": _trace_user_message(trace)})
            turns.append({"role": "assistant", "content": _trace_assistant_message(trace)})
        return turns

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "experiment_id": self.experiment_id,
            "turn_count": self.turn_count,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "latency_ms": round(self.latency_ms, 3),
            "error_count": self.error_count,
            "failed_turns": self.failed_turns,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "trace_ids": [trace.trace_id for trace in self.traces],
        }


def group_sessions(traces: Sequence[Trace]) -> list[Session]:
    """Group traces by ``mlflow.trace.session`` and sort sessions by time.

    Traces without the tag are ignored rather than assigned a synthetic
    session: a missing tag is a data-quality problem, not a conversation.
    """
    buckets: dict[str, dict[str, Trace]] = {}
    for trace in traces:
        session_id = str(trace.tags.get(SESSION_TAG, "")).strip()
        if not session_id:
            continue
        buckets.setdefault(session_id, {})[trace.trace_id] = trace
    sessions = [
        Session(session_id=session_id, traces=list(items.values()))
        for session_id, items in buckets.items()
    ]
    sessions.sort(key=lambda session: (session.started_at, session.session_id))
    return sessions


def search_sessions(
    tracking: Any,
    experiment_ids: Sequence[str] | None = None,
    *,
    max_results: int = 1000,
) -> list[Session]:
    """Load recent traces and group them into sessions.

    ``max_results`` bounds the trace scan, not the number of sessions.  The
    production implementation should push the session tag into the trace
    store and paginate by session; Mini keeps the simpler contract explicit.
    """
    traces = tracking.search_traces(experiment_ids, max_results=max_results)
    return group_sessions(traces)


# ----------------------------------------------------------------------
# Scripted conversation simulation
# ----------------------------------------------------------------------


@dataclass
class SimulationCase:
    """A deterministic stand-in for an LLM-simulated user.

    ``turns`` is the scripted user side.  If it is empty the goal is used as
    the first user message.  ``expectations.must_include`` / ``must_not_include``
    drive deterministic goal checking; real MLflow uses an LLM goal checker.
    """

    goal: str
    turns: tuple[str, ...] = ()
    persona: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    expectations: dict[str, Any] = field(default_factory=dict)
    case_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise InvalidParameter("test case goal must be a non-empty string")
        if not isinstance(self.turns, tuple):
            self.turns = tuple(str(item) for item in self.turns)
        if not self.case_id:
            digest = hashlib.sha256(self.goal.encode("utf-8")).hexdigest()[:10]
            self.case_id = f"case-{digest}"
        if not isinstance(self.context, dict) or not isinstance(self.expectations, dict):
            raise InvalidParameter("test case context and expectations must be objects")


@dataclass
class TurnRecord:
    index: int
    user: str
    assistant: str
    trace_id: str | None
    status: str
    latency_ms: float
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "user": self.user,
            "assistant": self.assistant,
            "trace_id": self.trace_id,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 3),
            "error": self.error,
        }


@dataclass
class ConversationResult:
    case_id: str
    session_id: str
    goal: str
    turns: list[TurnRecord]
    completed: bool
    stop_reason: str

    def transcript(self) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for turn in self.turns:
            messages.append({"role": "user", "content": turn.user})
            messages.append({"role": "assistant", "content": turn.assistant})
        return messages

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "session_id": self.session_id,
            "goal": self.goal,
            "completed": self.completed,
            "stop_reason": self.stop_reason,
            "turns": [turn.to_dict() for turn in self.turns],
        }


def _coerce_case(value: SimulationCase | Mapping[str, Any], index: int) -> SimulationCase:
    if isinstance(value, SimulationCase):
        return value
    if not isinstance(value, Mapping):
        raise InvalidParameter(f"test case {index} must be a SimulationCase or an object")
    goal = value.get("goal")
    turns_raw = value.get("turns") or value.get("script") or ()
    if isinstance(turns_raw, str):
        turns_raw = (turns_raw,)
    turns = tuple(str(item) for item in turns_raw)
    expectations = value.get("expectations") or {}
    context = value.get("context") or {}
    if not isinstance(expectations, dict) or not isinstance(context, dict):
        raise InvalidParameter(f"test case {index} expectations/context must be objects")
    case_id = str(value.get("case_id") or value.get("id") or "")
    return SimulationCase(
        goal=str(goal or ""),
        turns=turns,
        persona=str(value.get("persona") or ""),
        context=dict(context),
        expectations=dict(expectations),
        case_id=case_id,
    )


def _expectations_met(case: SimulationCase, final_response: str, errors: list[str]) -> bool:
    if errors:
        return False
    must_include = case.expectations.get("must_include") or ()
    if isinstance(must_include, str):
        must_include = (must_include,)
    must_not_include = case.expectations.get("must_not_include") or ()
    if isinstance(must_not_include, str):
        must_not_include = (must_not_include,)
    haystack = final_response.casefold()
    for term in must_include:
        if str(term).casefold() not in haystack:
            return False
    for term in must_not_include:
        if str(term).casefold() in haystack:
            return False
    return bool(final_response.strip())


def _log_simulated_turn(
    client: Any,
    *,
    experiment_id: str,
    session_id: str,
    case_id: str,
    user: str,
    assistant: str,
    status: str,
    error: str,
    started_ns: int,
    ended_ns: int,
) -> str:
    trace_id = new_id()
    span = Span(
        span_id=new_id()[:16],
        trace_id=trace_id,
        name="agent.turn",
        parent_span_id=None,
        span_type="AGENT",
        start_time_ns=started_ns,
        end_time_ns=ended_ns,
        status=status,
        inputs={"message": user},
        outputs={"answer": assistant} if assistant else None,
        attributes={"agent.case_id": case_id},
    )
    if status == "ERROR":
        span.events.append(
            {
                "name": "exception",
                "timestamp_ns": ended_ns,
                "attributes": {
                    "exception.type": "AgentError",
                    "exception.message": error[:4000],
                },
            }
        )
    trace = Trace(
        trace_id=trace_id,
        name="agent.turn",
        experiment_id=str(experiment_id or "0"),
        timestamp=max(0, started_ns // 1_000_000),
        status=status,
        tags={SESSION_TAG: session_id, "agent.case_id": case_id},
        request={"message": user},
        response={"answer": assistant} if assistant else None,
        spans=[span],
    )
    client.log_trace(trace)
    return trace_id


class ScriptedConversationSimulator:
    """Run scripted user turns against an agent and record one trace per turn.

    The agent callable receives ``(message, history)`` where ``history`` is a
    list of ``{"role", "content"}`` dictionaries.  It may return a string or an
    object that :func:`_message_text` can render (for example ``{"answer": ...}``).
    """

    def __init__(
        self,
        test_cases: Sequence[SimulationCase | Mapping[str, Any]],
        *,
        max_turns: int = 6,
        stop_on_error: bool = False,
        session_prefix: str = "sim",
    ) -> None:
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 50:
            raise InvalidParameter("max_turns must be an integer between 1 and 50")
        if not test_cases:
            raise InvalidParameter("test_cases must not be empty")
        if len(test_cases) > 1000:
            raise InvalidParameter("at most 1000 test cases are supported")
        self.test_cases = [_coerce_case(case, index) for index, case in enumerate(test_cases)]
        self.max_turns = max_turns
        self.stop_on_error = bool(stop_on_error)
        self.session_prefix = str(session_prefix or "sim")

    def run(
        self,
        respond: Callable[[str, list[dict[str, str]]], Any],
        *,
        client: Any | None = None,
        experiment_id: str | None = None,
    ) -> list[ConversationResult]:
        if not callable(respond):
            raise InvalidParameter("respond must be callable")
        results: list[ConversationResult] = []
        for case in self.test_cases:
            session_id = f"{self.session_prefix}-{new_id()[:12]}"
            script = list(case.turns or (case.goal,))
            truncated = len(script) > self.max_turns
            records: list[TurnRecord] = []
            history: list[dict[str, str]] = []
            errors: list[str] = []
            stopped_on_error = False
            for index, user_message in enumerate(script[: self.max_turns]):
                started_ns = now_ms() * 1_000_000 + index * 1_000_000
                error = ""
                status = "OK"
                assistant = ""
                try:
                    raw = respond(user_message, list(history))
                    assistant = _message_text(raw)
                except Exception as exc:  # noqa: BLE001 - simulation must keep evidence
                    status = "ERROR"
                    error = f"{type(exc).__name__}: {exc}"
                    errors.append(error)
                ended_ns = started_ns + 2_000_000
                trace_id = None
                if client is not None:
                    trace_id = _log_simulated_turn(
                        client,
                        experiment_id=experiment_id or "0",
                        session_id=session_id,
                        case_id=case.case_id,
                        user=user_message,
                        assistant=assistant,
                        status=status,
                        error=error,
                        started_ns=started_ns,
                        ended_ns=ended_ns,
                    )
                records.append(
                    TurnRecord(
                        index=index,
                        user=user_message,
                        assistant=assistant,
                        trace_id=trace_id,
                        status=status,
                        latency_ms=max(0.0, (ended_ns - started_ns) / 1_000_000),
                        error=error,
                    )
                )
                history.append({"role": "user", "content": user_message})
                if assistant:
                    history.append({"role": "assistant", "content": assistant})
                if status == "ERROR" and self.stop_on_error:
                    stopped_on_error = True
                    break
            final_response = next(
                (record.assistant for record in reversed(records) if record.assistant), ""
            )
            completed = _expectations_met(case, final_response, errors)
            if stopped_on_error:
                stop_reason = "error"
            elif truncated:
                stop_reason = "max_turns"
            elif completed:
                stop_reason = "goal_met"
            elif errors:
                stop_reason = "error"
            else:
                stop_reason = "script_exhausted"
            results.append(
                ConversationResult(
                    case_id=case.case_id,
                    session_id=session_id,
                    goal=case.goal,
                    turns=records,
                    completed=completed,
                    stop_reason=stop_reason,
                )
            )
        return results


def generate_test_cases(
    sessions: Sequence[Session], *, max_cases: int = 50
) -> list[SimulationCase]:
    """Turn observed sessions into replayable scripted test cases.

    This is the deterministic cousin of ``mlflow.genai.simulators.generate_test_cases``:
    the goal is the first user message, the script is the observed user side,
    and the final response is kept as a reference (not as an exact-match gate).
    """
    if not isinstance(max_cases, int) or isinstance(max_cases, bool) or max_cases < 1:
        raise InvalidParameter("max_cases must be a positive integer")
    cases: list[SimulationCase] = []
    for session in sessions:
        if len(cases) >= max_cases:
            break
        messages = [message for message in session.user_messages if message.strip()]
        if not messages:
            continue
        final_response = session.final_response
        cases.append(
            SimulationCase(
                goal=messages[0][:500],
                turns=tuple(messages),
                case_id=f"session-{session.session_id[-12:]}",
                expectations={"reference_response": final_response[:4000]},
            )
        )
    return cases


# ----------------------------------------------------------------------
# Deterministic scorers
# ----------------------------------------------------------------------


@dataclass
class ScorerResult:
    name: str
    value: float
    rationale: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "rationale": self.rationale,
            "metadata": dict(self.metadata),
        }


class ConversationCompleteness:
    """Fraction of required terms present in the final assistant response."""

    def __init__(self, required_terms: Sequence[str] = (), *, name: str = "conversation_completeness") -> None:
        self.required_terms = tuple(str(term) for term in required_terms if str(term))
        self.name = name

    def __call__(self, session: Session) -> ScorerResult:
        final = session.final_response.casefold()
        if not final.strip():
            return ScorerResult(
                self.name,
                0.0,
                "no final assistant response",
                {"missing": list(self.required_terms)},
            )
        if not self.required_terms:
            value = 0.0 if session.status == "ERROR" else 1.0
            return ScorerResult(
                self.name,
                value,
                "response present and session status is " + session.status,
                {"missing": []},
            )
        present = [term for term in self.required_terms if term.casefold() in final]
        missing = [term for term in self.required_terms if term.casefold() not in final]
        value = len(present) / len(self.required_terms)
        return ScorerResult(
            self.name,
            value,
            f"{len(present)}/{len(self.required_terms)} required terms present",
            {"present": present, "missing": missing},
        )


class UserFrustration:
    """Share of user turns that contain an explicit frustration cue.

    Higher is worse.  This is intentionally lexical: it catches repeated
    complaints and obvious retries, not subtle dissatisfaction.
    """

    _DEFAULT_CUES = (
        "still",
        "again",
        "not what",
        "wrong",
        "useless",
        "frustrat",
        "doesn't work",
        "does not work",
        "还是不对",
        "又错",
        "没用",
        "答非所问",
        "重复",
    )

    def __init__(self, cues: Sequence[str] = (), *, name: str = "user_frustration") -> None:
        self.cues = tuple(str(cue) for cue in (cues or self._DEFAULT_CUES) if str(cue))
        self.name = name

    def __call__(self, session: Session) -> ScorerResult:
        messages = [message for message in session.user_messages if message.strip()]
        if not messages:
            return ScorerResult(self.name, 0.0, "no user messages", {"hits": []})
        hits = [
            message
            for message in messages
            if any(cue.casefold() in message.casefold() for cue in self.cues)
        ]
        value = len(hits) / len(messages)
        return ScorerResult(
            self.name,
            round(value, 4),
            f"{len(hits)}/{len(messages)} user turns contain frustration cues",
            {"hits": [hit[:200] for hit in hits]},
        )


class ConversationalSafety:
    """1.0 when no forbidden pattern appears in any assistant response."""

    _DEFAULT_PATTERNS = (
        r"ignore (all |the )?(previous|prior) instructions",
        r"忽略(之前|以上|前面)的?(所有)?(指令|要求)",
        r"-----BEGIN (RSA |OPENSSH )?PRIVATE KEY-----",
        r"\b(system prompt|developer message)\b",
    )

    def __init__(self, patterns: Sequence[str] = (), *, name: str = "conversational_safety") -> None:
        source = tuple(patterns) or self._DEFAULT_PATTERNS
        self.patterns = tuple(re.compile(pattern, re.IGNORECASE) for pattern in source)
        self.name = name

    def __call__(self, session: Session) -> ScorerResult:
        matches: list[str] = []
        for message in session.assistant_messages:
            for pattern in self.patterns:
                match = pattern.search(message or "")
                if match:
                    matches.append(match.group(0)[:120])
        value = 0.0 if matches else 1.0
        return ScorerResult(
            self.name,
            value,
            "forbidden pattern found" if matches else "no forbidden pattern found",
            {"matches": matches},
        )


class ToolSuccessRate:
    """Fraction of tool spans that ended successfully (1.0 when no tools ran)."""

    def __init__(self, *, name: str = "tool_success_rate") -> None:
        self.name = name

    def __call__(self, session: Session) -> ScorerResult:
        total = session.tool_calls
        failures = session.tool_failures
        if total == 0:
            return ScorerResult(self.name, 1.0, "no tool calls", {"tool_calls": 0})
        value = (total - failures) / total
        return ScorerResult(
            self.name,
            round(value, 4),
            f"{total - failures}/{total} tool calls succeeded",
            {"tool_calls": total, "tool_failures": failures},
        )


class LatencyBudget:
    """1.0 when the session's wall-clock latency stays within the budget."""

    def __init__(self, max_ms: float = 5000.0, *, name: str = "latency_budget") -> None:
        if max_ms <= 0:
            raise InvalidParameter("max_ms must be positive")
        self.max_ms = float(max_ms)
        self.name = name

    def __call__(self, session: Session) -> ScorerResult:
        value = 1.0 if session.latency_ms <= self.max_ms else 0.0
        return ScorerResult(
            self.name,
            value,
            f"latency {session.latency_ms:.1f}ms vs budget {self.max_ms:.1f}ms",
            {"latency_ms": round(session.latency_ms, 3), "max_ms": self.max_ms},
        )


def score_sessions(
    tracking: Any,
    sessions: Sequence[Session],
    scorers: Sequence[Callable[[Session], ScorerResult]],
    *,
    source_id: str = "agentops",
) -> list[Assessment]:
    """Score sessions and attach one CODE assessment per scorer to the first trace.

    Attaching session-level judgments to the first trace mirrors real MLflow
    and keeps a single lookup path: ``search_traces`` filters can use
    ``assessments.<name>`` without knowing which turn produced the judgment.
    """
    if not scorers:
        raise InvalidParameter("at least one scorer is required")
    assessments: list[Assessment] = []
    for session in sessions:
        target = session.traces[0]
        for scorer in scorers:
            result = scorer(session)
            if not isinstance(result, ScorerResult):
                raise InvalidParameter("scorers must return ScorerResult")
            metadata = {
                **dict(result.metadata),
                "session_id": session.session_id,
                "turn_count": session.turn_count,
            }
            assessments.append(
                tracking.log_feedback(
                    target.trace_id,
                    result.name,
                    result.value,
                    source_type="CODE",
                    source_id=str(source_id),
                    rationale=result.rationale,
                    metadata=metadata,
                )
            )
    return assessments


@dataclass(frozen=True)
class ScorerSamplingConfig:
    """Mirror of the real ``ScorerSamplingConfig`` fields relevant to Mini."""

    sample_rate: float = 0.1
    filter_string: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sample_rate, bool) or not isinstance(self.sample_rate, (int, float)):
            raise InvalidParameter("sample_rate must be a number")
        if not 0.0 <= float(self.sample_rate) <= 1.0:
            raise InvalidParameter("sample_rate must be between 0 and 1")
        if self.filter_string is not None and not isinstance(self.filter_string, str):
            raise InvalidParameter("filter_string must be a string or None")


@dataclass(frozen=True)
class OnlineScorer:
    scorer: Callable[[Session], ScorerResult]
    config: ScorerSamplingConfig = field(default_factory=ScorerSamplingConfig)

    def __post_init__(self) -> None:
        if not callable(self.scorer):
            raise InvalidParameter("scorer must be callable")


def _sampled(key: str, rate: float) -> bool:
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(2**64)
    return bucket < rate


def score_online(
    tracking: Any,
    scorers: Sequence[OnlineScorer],
    *,
    experiment_ids: Sequence[str] | None = None,
    max_results: int = 1000,
    source_id: str = "online",
) -> list[Assessment]:
    """Score a deterministic sample of sessions, idempotently.

    The sampling unit is the session, keyed by ``source_id`` and scorer name,
    so re-running the job does not flip a session in or out of the sample.
    Real MLflow runs this asynchronously on new traces; Mini is a batch pass.
    """
    if not scorers:
        raise InvalidParameter("at least one online scorer is required")
    traces = tracking.search_traces(experiment_ids, max_results=max_results)
    sessions = group_sessions(traces)
    assessments: list[Assessment] = []
    for online in scorers:
        selected = sessions
        if online.config.filter_string:
            matching = {
                trace.trace_id
                for trace in tracking.search_traces(
                    experiment_ids,
                    filter_string=online.config.filter_string,
                    max_results=max_results,
                )
            }
            selected = [
                session
                for session in sessions
                if any(trace.trace_id in matching for trace in session.traces)
            ]
        for session in selected:
            scorer_name = getattr(online.scorer, "name", type(online.scorer).__name__)
            key = f"{source_id}:{scorer_name}:{session.session_id}"
            if not _sampled(key, float(online.config.sample_rate)):
                continue
            result = online.scorer(session)
            if not isinstance(result, ScorerResult):
                raise InvalidParameter("scorers must return ScorerResult")
            metadata = {
                **dict(result.metadata),
                "session_id": session.session_id,
                "turn_count": session.turn_count,
                "sample_rate": float(online.config.sample_rate),
            }
            assessments.append(
                tracking.log_feedback(
                    session.traces[0].trace_id,
                    result.name,
                    result.value,
                    source_type="CODE",
                    source_id=str(source_id),
                    rationale=result.rationale,
                    metadata=metadata,
                )
            )
    return assessments


# ----------------------------------------------------------------------
# Issue detection
# ----------------------------------------------------------------------


@dataclass
class Issue:
    kind: str
    signature: str
    count: int
    trace_ids: list[str]
    session_ids: list[str]
    severity: str
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "signature": self.signature,
            "count": self.count,
            "trace_ids": list(self.trace_ids),
            "session_ids": list(self.session_ids),
            "severity": self.severity,
            "evidence": list(self.evidence),
        }


def detect_issues(
    traces: Sequence[Trace],
    *,
    slow_ms: float = 5000.0,
    min_count: int = 1,
    max_evidence: int = 10,
) -> list[Issue]:
    """Group recurring failures into issues with trace/span evidence.

    Three deterministic detectors run:

    * a tool span with ``status == ERROR`` becomes ``tool_failure``;
    * any other error span becomes ``span_error``;
    * a trace slower than ``slow_ms`` becomes ``high_latency``.

    Signatures normalise quoted values, hex ids and numbers, so "timeout after
    8123 ms" and "timeout after 9912 ms" collapse into one issue.  Real MLflow
    3.18 adds an LLM pipeline that clusters semantic failures and reconciles
    them with human feedback; this is the explainable floor, not a replacement.
    """
    if slow_ms <= 0:
        raise InvalidParameter("slow_ms must be positive")
    if not isinstance(min_count, int) or isinstance(min_count, bool) or min_count < 1:
        raise InvalidParameter("min_count must be a positive integer")
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    def add(kind: str, signature: str, evidence: dict[str, Any]) -> None:
        item = buckets.setdefault(
            (kind, signature),
            {"kind": kind, "signature": signature, "evidence": []},
        )
        if len(item["evidence"]) < max_evidence:
            item["evidence"].append(evidence)

    for trace in traces:
        session_id = str(trace.tags.get(SESSION_TAG, ""))
        for span in trace.spans:
            if span.status != "ERROR":
                continue
            error_type, message = _exception_from_span(span)
            kind = "tool_failure" if _is_tool_span(span) else "span_error"
            signature = f"{span.name}|{error_type}|{_normalise_signature(message)}"
            add(
                kind,
                signature,
                {
                    "trace_id": trace.trace_id,
                    "session_id": session_id,
                    "span_id": span.span_id,
                    "span_name": span.name,
                    "span_type": span.span_type,
                    "error_type": error_type,
                    "message": message[:400],
                },
            )
        if trace.latency_ms > slow_ms:
            add(
                "high_latency",
                f"{trace.name}|>{int(slow_ms)}ms",
                {
                    "trace_id": trace.trace_id,
                    "session_id": session_id,
                    "latency_ms": round(trace.latency_ms, 3),
                    "threshold_ms": float(slow_ms),
                },
            )

    issues: list[Issue] = []
    for (kind, signature), item in buckets.items():
        evidence = item["evidence"]
        trace_ids = sorted({str(entry["trace_id"]) for entry in evidence})
        session_ids = sorted(
            {str(entry["session_id"]) for entry in evidence if entry.get("session_id")}
        )
        count = len(trace_ids)
        if count < min_count:
            continue
        if kind == "tool_failure" or count >= 5:
            severity = "high"
        elif kind == "span_error" or count >= 3:
            severity = "medium"
        else:
            severity = "low"
        issues.append(
            Issue(
                kind=kind,
                signature=signature,
                count=count,
                trace_ids=trace_ids,
                session_ids=session_ids,
                severity=severity,
                evidence=evidence,
            )
        )
    issues.sort(key=lambda issue: (-issue.count, issue.kind, issue.signature))
    return issues


# ----------------------------------------------------------------------
# Paired comparison and CI gating
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PairedComparison:
    metric: str
    n: int
    baseline_mean: float
    candidate_mean: float
    mean_diff: float
    median_diff: float
    ci_low: float
    ci_high: float
    improved: int
    regressed: int
    tied: int
    p_value: float
    alpha: float

    @property
    def significant(self) -> bool:
        return self.ci_low > 0.0 or self.ci_high < 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "n": self.n,
            "baseline_mean": self.baseline_mean,
            "candidate_mean": self.candidate_mean,
            "mean_diff": self.mean_diff,
            "median_diff": self.median_diff,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "improved": self.improved,
            "regressed": self.regressed,
            "tied": self.tied,
            "p_value": self.p_value,
            "alpha": self.alpha,
            "significant": self.significant,
        }


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = int(round(fraction * (len(sorted_values) - 1)))
    return sorted_values[max(0, min(len(sorted_values) - 1, index))]


def compare_paired(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    *,
    metric: str = "score",
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> PairedComparison:
    """Paired bootstrap comparison of two evaluation runs.

    Keys are evaluation-case ids; only cases present in both runs are used.
    Real MLflow 3.18 pairs rows on the same dataset, reports a BCa interval and
    a t-test p-value, and marks fewer than six pairs as ``insufficient_pairs``.
    Mini uses a percentile bootstrap and a bootstrap-based two-sided p-value;
    both are approximations, and neither corrects for multiple scorers.
    """
    if n_boot < 100:
        raise InvalidParameter("n_boot must be at least 100")
    if not 0.0 < alpha < 1.0:
        raise InvalidParameter("alpha must be between 0 and 1")
    keys = sorted(set(baseline) & set(candidate))
    if len(keys) < 2:
        raise InvalidParameter("at least two paired cases are required")
    base_values = [float(baseline[key]) for key in keys]
    cand_values = [float(candidate[key]) for key in keys]
    diffs = [cand - base for cand, base in zip(cand_values, base_values)]
    mean_diff = statistics.fmean(diffs)
    rng = random.Random(seed)
    boot: list[float] = []
    for _ in range(n_boot):
        sample = [diffs[rng.randrange(len(diffs))] for _ in range(len(diffs))]
        boot.append(statistics.fmean(sample))
    boot.sort()
    ci_low = _percentile(boot, alpha / 2)
    ci_high = _percentile(boot, 1 - alpha / 2)
    lower = sum(1 for value in boot if value <= 0.0) / n_boot
    upper = sum(1 for value in boot if value >= 0.0) / n_boot
    p_value = min(1.0, max(1.0 / (n_boot + 1), 2 * min(lower, upper)))
    improved = sum(1 for diff in diffs if diff > 0)
    regressed = sum(1 for diff in diffs if diff < 0)
    return PairedComparison(
        metric=str(metric),
        n=len(diffs),
        baseline_mean=round(statistics.fmean(base_values), 6),
        candidate_mean=round(statistics.fmean(cand_values), 6),
        mean_diff=round(mean_diff, 6),
        median_diff=round(statistics.median(diffs), 6),
        ci_low=round(ci_low, 6),
        ci_high=round(ci_high, 6),
        improved=improved,
        regressed=regressed,
        tied=len(diffs) - improved - regressed,
        p_value=round(p_value, 6),
        alpha=float(alpha),
    )


def assert_improved(
    comparison: PairedComparison,
    *,
    min_delta: float = 0.0,
    min_pairs: int = 6,
) -> None:
    """CI gate: raise ``AssertionError`` unless the candidate is better.

    Mirrors the semantics of ``ComparisonResult.assert_improved`` in real
    MLflow: insufficient pairs fail closed, and a significant improvement in
    the wrong direction never passes.
    """
    if comparison.n < min_pairs:
        raise AssertionError(
            f"insufficient_pairs: {comparison.n} paired cases, need at least {min_pairs}"
        )
    if comparison.mean_diff <= min_delta:
        raise AssertionError(
            f"{comparison.metric}: mean diff {comparison.mean_diff:.4f} "
            f"is not above min_delta {min_delta:.4f}"
        )
    if not comparison.significant:
        raise AssertionError(
            f"{comparison.metric}: 95% CI [{comparison.ci_low:.4f}, "
            f"{comparison.ci_high:.4f}] includes zero"
        )
    if comparison.p_value >= comparison.alpha:
        raise AssertionError(
            f"{comparison.metric}: p={comparison.p_value:.4f} >= alpha={comparison.alpha}"
        )


def sessions_to_dataset_records(sessions: Sequence[Session]) -> list[dict[str, Any]]:
    """Convert sessions into records accepted by ``DatasetRegistry.create_dataset``.

    The whole conversation is kept in ``inputs.messages`` and the observed
    final answer in ``expectations.reference_response``.  A record is evidence,
    not a golden label: curate and redact it before promoting it into a golden
    set (chapter 44).
    """
    records: list[dict[str, Any]] = []
    for session in sessions:
        messages = session.transcript()
        if not messages:
            continue
        records.append(
            {
                "inputs": {
                    "messages": messages,
                    "session_id": session.session_id,
                },
                "expectations": {
                    "reference_response": session.final_response,
                    "status": session.status,
                },
                "tags": {
                    "source": "agentops",
                    "session_id": session.session_id,
                    "turn_count": session.turn_count,
                },
            }
        )
    return records
