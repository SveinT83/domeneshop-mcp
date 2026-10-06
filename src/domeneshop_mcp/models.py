"""Validated API payloads. No user-supplied API URLs or path fragments."""

import ipaddress
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RecordType = Literal["A", "AAAA", "CNAME", "ANAME", "MX", "SRV", "TLSA", "TXT", "DS", "CAA", "NS"]
DomainRef = str | Annotated[int, Field(gt=0, strict=True)]
PositiveId = Annotated[int, Field(gt=0, strict=True)]
UInt16 = Annotated[int, Field(ge=0, le=65535, strict=True)]


def hostname(value: str, *, relative: bool = False) -> str:
    value = value.strip().rstrip(".").lower()
    if relative and value == "@":
        return value
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("Invalid international hostname") from None
    if not value or len(value) > 253:
        raise ValueError("Hostname must contain 1-253 characters")
    for index, label in enumerate(value.split(".")):
        if relative and index == 0 and label == "*":
            continue
        if not re.fullmatch(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", label):
            raise ValueError("Invalid hostname label")
    return value


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DNSRecord(Payload):
    host: str = "@"
    type: RecordType
    data: str = Field(max_length=65535)
    ttl: Annotated[int, Field(ge=60, le=604800, multiple_of=60, strict=True)] = 3600
    priority: UInt16 | None = None
    weight: UInt16 | None = None
    port: UInt16 | None = None
    usage: Annotated[int, Field(ge=0, le=3, strict=True)] | None = None
    selector: Annotated[int, Field(ge=0, le=1, strict=True)] | None = None
    dtype: Annotated[int, Field(ge=0, le=2, strict=True)] | None = None
    tag: int | str | None = None
    alg: Annotated[int, Field(ge=0, le=255, strict=True)] | None = None
    digest: Annotated[int, Field(ge=0, le=255, strict=True)] | None = None
    flags: Annotated[int, Field(ge=0, le=255, strict=True)] | None = None

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        return hostname(value, relative=True)

    @model_validator(mode="after")
    def valid_record(self):
        required = {
            "MX": {"priority"},
            "SRV": {"priority", "weight", "port"},
            "TLSA": {"usage", "selector", "dtype"},
            "DS": {"tag", "alg", "digest"},
            "CAA": {"flags", "tag"},
        }.get(self.type, set())
        provided = {
            k
            for k in (
                "priority",
                "weight",
                "port",
                "usage",
                "selector",
                "dtype",
                "tag",
                "alg",
                "digest",
                "flags",
            )
            if getattr(self, k) is not None
        }
        if provided != required:
            raise ValueError(f"{self.type} requires exactly these extra fields: {sorted(required)}")
        if self.type in {"A", "AAAA"}:
            ip = ipaddress.ip_address(self.data)
            if ip.version != (4 if self.type == "A" else 6):
                raise ValueError("IP address does not match record type")
            object.__setattr__(self, "data", str(ip))
        elif self.type in {"CNAME", "ANAME", "MX", "NS", "SRV"}:
            target = "." if self.data == "." and self.type in {"MX", "SRV"} else hostname(self.data)
            object.__setattr__(self, "data", target)
        elif self.type in {"TLSA", "DS"}:
            if not re.fullmatch(r"(?:[a-fA-F0-9]{2})+", self.data):
                raise ValueError("TLSA/DS data must be nonempty hexadecimal bytes")
            if self.type == "TLSA" and self.dtype in {1, 2}:
                if len(self.data) != {1: 64, 2: 128}[self.dtype]:
                    raise ValueError("TLSA hash length does not match dtype")
            object.__setattr__(self, "data", self.data.upper())
        if self.type == "DS" and (type(self.tag) is not int or not 0 <= self.tag <= 65535):
            raise ValueError("DS tag must be an integer from 0 to 65535")
        if self.type == "CAA" and (
            not isinstance(self.tag, str) or not re.fullmatch(r"[a-zA-Z0-9]+", self.tag)
        ):
            raise ValueError("CAA tag must be an alphanumeric string, e.g. issue")
        return self

    def payload(self) -> dict:
        return self.model_dump(exclude_none=True)


class Forward(Payload):
    host: str = "@"
    url: str = Field(min_length=1, max_length=8192)
    frame: bool = False

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        return hostname(value, relative=True)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https", "ftp"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Forward URL must be http(s)/ftp without embedded credentials")
        return value


class DNSChange(Payload):
    action: Literal["create", "update", "delete"]
    record_id: PositiveId | None = None
    record: DNSRecord | None = None

    @model_validator(mode="after")
    def valid_change(self):
        if (self.action == "create") != (self.record_id is None):
            raise ValueError("Only update/delete require record_id")
        if (self.action == "delete") != (self.record is None):
            raise ValueError("Only create/update require record")
        return self
