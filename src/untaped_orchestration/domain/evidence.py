import re
from enum import StrEnum
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, RootModel, field_validator

GITHUB_NUMBER_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)"
)
GITHUB_RELEASE_RE = re.compile(r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)@(?P<tag>\S+)")
GITHUB_COMMIT_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)@(?P<sha>[0-9A-Fa-f]{40})"
)
PYPI_RE = re.compile(r"(?P<project>[A-Za-z0-9]+(?:[-_.]+[A-Za-z0-9]+)*)@(?P<version>\S+)")
OPAQUE_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9-]*):(?P<payload>\S+)")
PEP_503_RUN_RE = re.compile(r"[-_.]+")


class EvidenceRelation(StrEnum):
    TRACKED_BY = "tracked-by"
    IMPLEMENTED_BY = "implemented-by"
    VERIFIED_BY = "verified-by"
    RELEASED_AS = "released-as"
    PUBLISHED_AS = "published-as"


def _canonical_github_number(scheme: str, payload: str) -> str:
    match = GITHUB_NUMBER_RE.fullmatch(payload)
    if match is None:
        raise ValueError(f"invalid {scheme} reference")
    return (
        f"{scheme}:{match.group('owner').lower()}/{match.group('repo').lower()}"
        f"#{match.group('number')}"
    )


def _canonical_github_release(payload: str) -> str:
    match = GITHUB_RELEASE_RE.fullmatch(payload)
    if match is None:
        raise ValueError("invalid github-release reference")
    return (
        "github-release:"
        f"{match.group('owner').lower()}/{match.group('repo').lower()}@{match.group('tag')}"
    )


def _canonical_github_commit(payload: str) -> str:
    match = GITHUB_COMMIT_RE.fullmatch(payload)
    if match is None:
        raise ValueError("invalid github-commit reference")
    return (
        "github-commit:"
        f"{match.group('owner').lower()}/{match.group('repo').lower()}@{match.group('sha').lower()}"
    )


def _canonical_pypi(payload: str) -> str:
    match = PYPI_RE.fullmatch(payload)
    if match is None:
        raise ValueError("invalid pypi reference")
    project = PEP_503_RUN_RE.sub("-", match.group("project")).lower()
    return f"pypi:{project}@{match.group('version')}"


def _canonical_url(payload: str) -> str:
    if any(character.isspace() for character in payload):
        raise ValueError("URL evidence cannot contain whitespace")
    parsed = urlsplit(payload)
    if parsed.scheme.lower() != "https" or parsed.hostname is None:
        raise ValueError("URL evidence must use HTTPS and include a host")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL evidence has an invalid port") from error

    userinfo = ""
    if "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", maxsplit=1)[0] + "@"
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{userinfo}{host}"
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    canonical = SplitResult("https", netloc, parsed.path, parsed.query, parsed.fragment)
    return f"url:{urlunsplit(canonical)}"


def canonicalize_evidence_reference(value: str) -> str:
    scheme, separator, payload = value.partition(":")
    if not separator:
        raise ValueError("evidence reference must contain a scheme")
    if scheme in {"github-pr", "github-issue"}:
        return _canonical_github_number(scheme, payload)
    if scheme == "github-release":
        return _canonical_github_release(payload)
    if scheme == "github-commit":
        return _canonical_github_commit(payload)
    if scheme == "pypi":
        return _canonical_pypi(payload)
    if scheme == "url":
        return _canonical_url(payload)
    if OPAQUE_RE.fullmatch(value) is None:
        raise ValueError("invalid opaque evidence reference")
    return value


class EvidenceReference(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def _canonicalize_reference(cls, value: str) -> str:
        return canonicalize_evidence_reference(value)

    def __str__(self) -> str:
        return self.root


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relation: EvidenceRelation
    reference: EvidenceReference
