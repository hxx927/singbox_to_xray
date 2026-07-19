#!/usr/bin/env python3
"""Convert S-UI/sing-box inbounds and safely deploy them to miaomiaowuX Xray."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


VERSION = "0.4.3"
DEFAULT_SINGBOX_CONFIG = "/usr/local/etc/sing-box/config.json"
DEFAULT_SUI_DB = "/usr/local/s-ui/db/s-ui.db"
DEFAULT_XRAY_CONFIG = "/usr/local/etc/xray/config.json"
DEFAULT_STATE_FILE = "/var/lib/mmwx-singbox-migrate/state.json"
DEFAULT_AGENT_CONFIG = "/etc/mmw-agent/config.yaml"
AGENT_USER_AGENT = "miaomiaowux/0.1"
SUPPORTED_TYPES = {
    "vless",
    "vmess",
    "trojan",
    "shadowsocks",
    "hysteria2",
    "socks",
    "http",
}
SUI_USER_TYPES = {
    "mixed",
    "socks",
    "http",
    "shadowsocks",
    "vmess",
    "trojan",
    "naive",
    "hysteria",
    "shadowtls",
    "tuic",
    "hysteria2",
    "vless",
    "anytls",
}


class MigrationError(RuntimeError):
    pass


class MasterSyncUnavailable(MigrationError):
    pass


class UnsupportedInbound(MigrationError):
    pass


@dataclass
class ConversionOptions:
    strict: bool = False
    selected_tags: set[str] = field(default_factory=set)
    port_offset: int = 0
    port_map: dict[int, int] = field(default_factory=dict)
    tag_suffix: str = ""
    reality_public_keys: dict[str, str] = field(default_factory=dict)
    xray_bin: str = "xray"
    derive_reality_keys: bool = True


@dataclass
class ConversionResult:
    inbounds: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class SourceDocument:
    config: dict[str, Any]
    kind: str
    path: Path

    @property
    def inbound_count(self) -> int:
        inbounds = self.config.get("inbounds")
        return len(inbounds) if isinstance(inbounds, list) else 0

    @property
    def label(self) -> str:
        return "S-UI database" if self.kind == "s-ui-db" else "sing-box JSON"


def log(level: str, message: str) -> None:
    print(f"[{level}] {message}", file=sys.stderr)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MigrationError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MigrationError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise MigrationError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"top-level JSON must be an object: {path}")
    return value


def parse_database_json(value: Any, context: str, expected: type = dict) -> Any:
    if value in (None, ""):
        return expected()
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        parsed = json.loads(value) if isinstance(value, str) else value
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"invalid JSON in S-UI {context}: {exc}") from exc
    if not isinstance(parsed, expected):
        raise MigrationError(f"S-UI {context} must contain a {expected.__name__}")
    return parsed


def sui_client_config_key(inbound_type: str, inbound: dict[str, Any]) -> str:
    if inbound_type != "shadowsocks":
        return inbound_type
    method = nonempty_string(inbound.get("method"))
    if method == "2022-blake3-aes-128-gcm":
        return "shadowsocks16"
    if method in {"2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305"}:
        return "shadowsocks32"
    return "shadowsocks"


def strip_sui_vless_vision(user: Any) -> Any:
    # S-UI removes Vision when VLESS uses a transport or has no TLS.
    serialized = json.dumps(user, ensure_ascii=False)
    return json.loads(serialized.replace("xtls-rprx-vision", ""))


def load_sui_database(path: Path) -> dict[str, Any]:
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    except (OSError, sqlite3.Error) as exc:
        raise MigrationError(f"cannot open S-UI database {path}: {exc}") from exc

    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT id, type, tag, tls_id, options FROM inbounds ORDER BY id"
        ).fetchall()
        inbounds: list[dict[str, Any]] = []
        for row in rows:
            inbound_id = int(row["id"])
            inbound_type = nonempty_string(row["type"]).lower()
            tag = nonempty_string(row["tag"])
            context = f"inbound {tag or inbound_id!r}"
            inbound = parse_database_json(row["options"], f"{context} options")
            inbound["type"] = inbound_type
            inbound["tag"] = tag

            tls_id = int(row["tls_id"] or 0)
            if tls_id:
                tls_row = connection.execute("SELECT server FROM tls WHERE id = ?", (tls_id,)).fetchone()
                if tls_row is None:
                    raise MigrationError(f"S-UI {context} references missing TLS record {tls_id}")
                inbound["tls"] = parse_database_json(tls_row["server"], f"TLS record {tls_id}")

            if inbound_type in SUI_USER_TYPES:
                config_key = sui_client_config_key(inbound_type, inbound)
                users: list[Any] = []
                client_rows = connection.execute(
                    "SELECT config, inbounds FROM clients WHERE enable = 1 ORDER BY id"
                ).fetchall()
                for client_row in client_rows:
                    inbound_ids = parse_database_json(
                        client_row["inbounds"], "client inbound assignments", list
                    )
                    if str(inbound_id) not in {str(value) for value in inbound_ids}:
                        continue
                    client_config = parse_database_json(client_row["config"], "client config")
                    user = client_config.get(config_key)
                    if user is None:
                        continue
                    if isinstance(user, str):
                        try:
                            user = json.loads(user)
                        except json.JSONDecodeError:
                            pass
                    transport = inbound.get("transport")
                    has_transport = isinstance(transport, dict) and bool(
                        nonempty_string(transport.get("type"))
                    )
                    if inbound_type == "vless" and ("tls" not in inbound or has_transport):
                        user = strip_sui_vless_vision(user)
                    users.append(user)
                inbound["users"] = users
            inbounds.append(inbound)
    except sqlite3.Error as exc:
        raise MigrationError(f"cannot read S-UI database {path}: {exc}") from exc
    finally:
        connection.close()
    return {"inbounds": inbounds}


def choose_source(candidates: list[SourceDocument], interactive: bool) -> SourceDocument:
    if not candidates:
        raise MigrationError(
            "no inbound source found; pass --s-ui-db PATH or --input PATH"
        )
    if not interactive or len(candidates) == 1:
        return candidates[0]
    if not sys.stdin.isatty():
        raise MigrationError("--interactive requires a terminal")

    print("Detected inbound sources:", file=sys.stderr)
    for index, candidate in enumerate(candidates, 1):
        print(
            f"  {index}. {candidate.label}: {candidate.path} "
            f"({candidate.inbound_count} inbound(s))",
            file=sys.stderr,
        )
    while True:
        print(
            f"Select source [1-{len(candidates)}] (default 1): ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        answer = sys.stdin.readline()
        if answer == "":
            raise MigrationError("interactive source selection was cancelled")
        answer = answer.strip()
        if not answer:
            return candidates[0]
        try:
            selected = int(answer)
        except ValueError:
            selected = 0
        if 1 <= selected <= len(candidates):
            return candidates[selected - 1]
        print("Invalid selection.", file=sys.stderr)


def resolve_source(args: argparse.Namespace) -> SourceDocument:
    if args.input:
        path = Path(args.input)
        source = SourceDocument(load_json(path), "json", path)
    elif args.s_ui_db:
        path = Path(args.s_ui_db)
        source = SourceDocument(load_sui_database(path), "s-ui-db", path)
    else:
        candidates: list[SourceDocument] = []
        sui_path = Path(DEFAULT_SUI_DB)
        if sui_path.exists():
            sui_source = SourceDocument(load_sui_database(sui_path), "s-ui-db", sui_path)
            if sui_source.inbound_count:
                candidates.append(sui_source)

        json_path = Path(DEFAULT_SINGBOX_CONFIG)
        if json_path.exists():
            json_source = SourceDocument(load_json(json_path), "json", json_path)
            if json_source.inbound_count:
                candidates.append(json_source)
        source = choose_source(candidates, args.interactive)

    log(
        "SOURCE",
        f"selected {source.label} {source.path} ({source.inbound_count} inbound(s))",
    )
    return source


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def nonempty_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def require_string(obj: dict[str, Any], key: str, context: str) -> str:
    value = nonempty_string(obj.get(key))
    if not value:
        raise MigrationError(f"{context}: missing {key}")
    return value


def parse_port(value: Any, context: str) -> int:
    if isinstance(value, bool):
        raise MigrationError(f"{context}: invalid port {value!r}")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"{context}: invalid port {value!r}") from exc
    if port < 1 or port > 65535:
        raise MigrationError(f"{context}: port out of range: {port}")
    return port


def source_tag(inbound: dict[str, Any], index: int) -> str:
    tag = nonempty_string(inbound.get("tag"))
    if tag:
        return tag
    protocol = nonempty_string(inbound.get("type")) or "inbound"
    port = inbound.get("listen_port", "unknown")
    return f"{protocol}-{port}-{index + 1}"


def mapped_port(original: int, options: ConversionOptions, context: str) -> int:
    target = options.port_map.get(original, original + options.port_offset)
    return parse_port(target, context)


def client_email(user: dict[str, Any], tag: str, index: int) -> str:
    for key in ("name", "email", "username"):
        value = nonempty_string(user.get(key))
        if value:
            return value
    return f"{tag}-user-{index + 1}"


def copy_if_present(source: dict[str, Any], target: dict[str, Any], source_key: str, target_key: str) -> None:
    if source_key in source and source[source_key] not in (None, ""):
        target[target_key] = copy.deepcopy(source[source_key])


def derive_reality_public_key(private_key: str, xray_bin: str) -> str:
    binary = shutil.which(xray_bin) if os.path.sep not in xray_bin else xray_bin
    if not binary or not Path(binary).exists():
        return ""
    try:
        proc = subprocess.run(
            [binary, "x25519", "-i", private_key],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    for pattern in (
        r"(?im)^Password\s*\(PublicKey\):\s*(\S+)",
        r"(?im)^Password:\s*(\S+)",
        r"(?im)^Public key:\s*(\S+)",
    ):
        match = re.search(pattern, proc.stdout)
        if match:
            return match.group(1)
    return ""


def convert_transport(transport: Any, context: str, warnings: list[str]) -> dict[str, Any]:
    stream: dict[str, Any] = {"network": "tcp", "security": "none"}
    if not transport:
        return stream
    if not isinstance(transport, dict):
        raise MigrationError(f"{context}: transport must be an object")

    transport_type = nonempty_string(transport.get("type")).lower() or "tcp"
    if transport_type == "tcp":
        return stream
    if transport_type == "ws":
        settings: dict[str, Any] = {}
        copy_if_present(transport, settings, "path", "path")
        copy_if_present(transport, settings, "headers", "headers")
        if "max_early_data" in transport:
            settings["maxEarlyData"] = transport["max_early_data"]
        copy_if_present(transport, settings, "early_data_header_name", "earlyDataHeaderName")
        stream.update({"network": "ws", "wsSettings": settings})
        return stream
    if transport_type == "grpc":
        settings = {}
        copy_if_present(transport, settings, "service_name", "serviceName")
        if "idle_timeout" in transport:
            warnings.append(f"{context}: gRPC idle_timeout has no direct Xray inbound mapping")
        stream.update({"network": "grpc", "grpcSettings": settings})
        return stream
    if transport_type in {"http", "h2"}:
        settings = {}
        copy_if_present(transport, settings, "path", "path")
        host = transport.get("host")
        if host is None and isinstance(transport.get("headers"), dict):
            host = transport["headers"].get("Host") or transport["headers"].get("host")
        if host:
            settings["host"] = as_list(host)
        stream.update({"network": "http", "httpSettings": settings})
        return stream
    if transport_type in {"httpupgrade", "http_upgrade"}:
        settings = {}
        copy_if_present(transport, settings, "path", "path")
        copy_if_present(transport, settings, "host", "host")
        stream.update({"network": "httpupgrade", "httpupgradeSettings": settings})
        return stream
    raise UnsupportedInbound(f"{context}: unsupported transport type {transport_type!r}")


def convert_tls(
    tls: Any,
    stream: dict[str, Any],
    tag: str,
    context: str,
    options: ConversionOptions,
    warnings: list[str],
) -> None:
    if not tls:
        return
    if not isinstance(tls, dict):
        raise MigrationError(f"{context}: tls must be an object")
    if tls.get("enabled") is False:
        return

    reality = tls.get("reality")
    if isinstance(reality, dict) and reality.get("enabled", True):
        handshake = reality.get("handshake")
        if not isinstance(handshake, dict):
            raise MigrationError(f"{context}: REALITY handshake is required")
        destination_host = require_string(handshake, "server", f"{context} REALITY handshake")
        destination_port = parse_port(handshake.get("server_port", 443), f"{context} REALITY handshake")
        private_key = require_string(reality, "private_key", f"{context} REALITY")
        short_ids = [str(item) for item in as_list(reality.get("short_id"))]
        if not short_ids:
            raise MigrationError(f"{context}: REALITY short_id is required")

        server_names = [str(item) for item in as_list(tls.get("server_name")) if str(item)]
        if not server_names:
            server_names = [destination_host]

        public_key = options.reality_public_keys.get(tag, "")
        if not public_key:
            public_key = nonempty_string(reality.get("public_key"))
        if not public_key and options.derive_reality_keys:
            public_key = derive_reality_public_key(private_key, options.xray_bin)
        if not public_key:
            message = (
                f"{context}: REALITY public key is unavailable; pass "
                f"--reality-public-key {tag}=PUBLIC_KEY or install xray for derivation"
            )
            if options.strict:
                raise MigrationError(message)
            warnings.append(message)

        settings: dict[str, Any] = {
            "show": False,
            "dest": f"{destination_host}:{destination_port}",
            "xver": 0,
            "serverNames": server_names,
            "privateKey": private_key,
            "shortIds": short_ids,
        }
        if public_key:
            settings["publicKey"] = public_key
        copy_if_present(reality, settings, "max_time_difference", "maxTimeDiff")
        stream["security"] = "reality"
        stream["realitySettings"] = settings
        return

    if tls.get("acme"):
        raise UnsupportedInbound(f"{context}: sing-box ACME settings must be migrated as certificate files")

    cert_path = nonempty_string(tls.get("certificate_path"))
    key_path = nonempty_string(tls.get("key_path"))
    certificate = tls.get("certificate")
    key = tls.get("key")
    if bool(cert_path) != bool(key_path):
        raise MigrationError(f"{context}: both certificate_path and key_path are required")
    if (certificate is None) != (key is None):
        raise MigrationError(f"{context}: both inline certificate and key are required")
    if not cert_path and certificate is None:
        raise MigrationError(f"{context}: TLS is enabled but no certificate/key was found")

    certificate_entry: dict[str, Any]
    if cert_path:
        certificate_entry = {"certificateFile": cert_path, "keyFile": key_path}
    else:
        certificate_entry = {"certificate": as_list(certificate), "key": as_list(key)}

    tls_settings: dict[str, Any] = {"certificates": [certificate_entry]}
    server_names = [str(item) for item in as_list(tls.get("server_name")) if str(item)]
    if server_names:
        tls_settings["serverName"] = server_names[0]
    alpn = [str(item) for item in as_list(tls.get("alpn")) if str(item)]
    if alpn:
        tls_settings["alpn"] = alpn
    stream["security"] = "tls"
    stream["tlsSettings"] = tls_settings


def add_sniffing(source: dict[str, Any], target: dict[str, Any]) -> None:
    if source.get("sniff") or source.get("sniff_override_destination"):
        target["sniffing"] = {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
        }


def convert_vless(source: dict[str, Any], tag: str, context: str) -> dict[str, Any]:
    clients: list[dict[str, Any]] = []
    for index, raw_user in enumerate(as_list(source.get("users"))):
        if not isinstance(raw_user, dict):
            raise MigrationError(f"{context}: users[{index}] must be an object")
        client = {
            "id": require_string(raw_user, "uuid", f"{context} users[{index}]"),
            "email": client_email(raw_user, tag, index),
            "level": 0,
        }
        copy_if_present(raw_user, client, "flow", "flow")
        clients.append(client)
    if not clients:
        raise MigrationError(f"{context}: at least one VLESS user is required")
    settings: dict[str, Any] = {"clients": clients, "decryption": "none"}
    copy_if_present(source, settings, "decryption", "decryption")
    copy_if_present(source, settings, "encryption", "encryption")
    return settings


def convert_vmess(source: dict[str, Any], tag: str, context: str) -> dict[str, Any]:
    clients: list[dict[str, Any]] = []
    for index, raw_user in enumerate(as_list(source.get("users"))):
        if not isinstance(raw_user, dict):
            raise MigrationError(f"{context}: users[{index}] must be an object")
        client = {
            "id": require_string(raw_user, "uuid", f"{context} users[{index}]"),
            "email": client_email(raw_user, tag, index),
            "level": 0,
            "alterId": int(raw_user.get("alter_id", 0)),
        }
        clients.append(client)
    if not clients:
        raise MigrationError(f"{context}: at least one VMess user is required")
    return {"clients": clients}


def convert_trojan(source: dict[str, Any], tag: str, context: str) -> dict[str, Any]:
    clients: list[dict[str, Any]] = []
    for index, raw_user in enumerate(as_list(source.get("users"))):
        if not isinstance(raw_user, dict):
            raise MigrationError(f"{context}: users[{index}] must be an object")
        client = {
            "password": require_string(raw_user, "password", f"{context} users[{index}]"),
            "email": client_email(raw_user, tag, index),
            "level": 0,
        }
        clients.append(client)
    if not clients:
        raise MigrationError(f"{context}: at least one Trojan user is required")
    return {"clients": clients}


def normalize_network(value: Any) -> str:
    if not value:
        return "tcp,udp"
    if isinstance(value, list):
        values = [str(item).lower() for item in value]
        return ",".join(values)
    return str(value).lower().replace(" ", "")


def convert_shadowsocks(source: dict[str, Any], tag: str, context: str) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "method": require_string(source, "method", context),
        "password": require_string(source, "password", context),
        "network": normalize_network(source.get("network")),
    }
    clients: list[dict[str, Any]] = []
    for index, raw_user in enumerate(as_list(source.get("users"))):
        if not isinstance(raw_user, dict):
            raise MigrationError(f"{context}: users[{index}] must be an object")
        clients.append(
            {
                "password": require_string(raw_user, "password", f"{context} users[{index}]"),
                "email": client_email(raw_user, tag, index),
                "level": 0,
            }
        )
    if clients:
        settings["clients"] = clients
    return settings


def convert_hysteria2(
    source: dict[str, Any], tag: str, context: str, warnings: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    clients: list[dict[str, Any]] = []
    for index, raw_user in enumerate(as_list(source.get("users"))):
        if not isinstance(raw_user, dict):
            raise MigrationError(f"{context}: users[{index}] must be an object")
        clients.append(
            {
                "auth": require_string(raw_user, "password", f"{context} users[{index}]"),
                "email": client_email(raw_user, tag, index),
                "level": 0,
            }
        )
    if not clients:
        raise MigrationError(f"{context}: at least one Hysteria2 user is required")

    hysteria_settings: dict[str, Any] = {"version": 2}
    obfs = source.get("obfs")
    if isinstance(obfs, dict):
        obfs_type = nonempty_string(obfs.get("type")).lower()
        if obfs_type and obfs_type != "salamander":
            raise UnsupportedInbound(f"{context}: unsupported Hysteria2 obfs {obfs_type!r}")
        password = nonempty_string(obfs.get("password"))
        if password:
            hysteria_settings["password"] = password
    for key in ("up_mbps", "down_mbps", "ignore_client_bandwidth"):
        if key in source:
            warnings.append(f"{context}: {key} is not copied; verify Xray congestion settings manually")
    stream = {
        "network": "hysteria",
        "security": "none",
        "hysteriaSettings": hysteria_settings,
    }
    return {"version": 2, "clients": clients}, stream


def convert_socks_or_http(
    source: dict[str, Any], protocol: str, tag: str, context: str
) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    for index, raw_user in enumerate(as_list(source.get("users"))):
        if not isinstance(raw_user, dict):
            raise MigrationError(f"{context}: users[{index}] must be an object")
        accounts.append(
            {
                "user": require_string(raw_user, "username", f"{context} users[{index}]"),
                "pass": require_string(raw_user, "password", f"{context} users[{index}]"),
            }
        )
    if protocol == "socks":
        return {
            "auth": "password" if accounts else "noauth",
            "accounts": accounts,
            "udp": True,
            "ip": "127.0.0.1",
        }
    return {"accounts": accounts, "allowTransparent": False}


def convert_one(
    source: dict[str, Any], index: int, options: ConversionOptions, warnings: list[str]
) -> dict[str, Any]:
    protocol = nonempty_string(source.get("type")).lower()
    original_tag = source_tag(source, index)
    context = f"inbound {original_tag!r}"
    if protocol not in SUPPORTED_TYPES:
        raise UnsupportedInbound(f"{context}: unsupported protocol {protocol or '<empty>'!r}")

    original_port = parse_port(source.get("listen_port"), context)
    port = mapped_port(original_port, options, context)
    tag = original_tag + options.tag_suffix
    if tag == "api":
        raise MigrationError(f"{context}: tag 'api' is reserved by miaomiaowuX")

    target: dict[str, Any] = {
        "tag": tag,
        "listen": nonempty_string(source.get("listen")) or "::",
        "port": port,
        "protocol": "hysteria" if protocol == "hysteria2" else protocol,
    }

    stream = convert_transport(source.get("transport"), context, warnings)
    if protocol == "vless":
        target["settings"] = convert_vless(source, tag, context)
    elif protocol == "vmess":
        target["settings"] = convert_vmess(source, tag, context)
    elif protocol == "trojan":
        target["settings"] = convert_trojan(source, tag, context)
    elif protocol == "shadowsocks":
        target["settings"] = convert_shadowsocks(source, tag, context)
    elif protocol == "hysteria2":
        target["settings"], stream = convert_hysteria2(source, tag, context, warnings)
    elif protocol in {"socks", "http"}:
        target["settings"] = convert_socks_or_http(source, protocol, tag, context)

    tls = source.get("tls")
    if protocol == "hysteria2" and not tls:
        raise MigrationError(f"{context}: Hysteria2 requires TLS")
    convert_tls(tls, stream, original_tag, context, options, warnings)
    if protocol == "hysteria2" and stream.get("security") != "tls":
        raise MigrationError(f"{context}: Hysteria2 TLS configuration is incomplete")

    if protocol not in {"socks", "http"} or source.get("transport") or source.get("tls"):
        target["streamSettings"] = stream
    add_sniffing(source, target)
    return target


def convert_config(config: dict[str, Any], options: ConversionOptions) -> ConversionResult:
    raw_inbounds = config.get("inbounds")
    if not isinstance(raw_inbounds, list):
        raise MigrationError("sing-box config must contain an inbounds array")

    result = ConversionResult()
    seen_tags: set[str] = set()
    seen_ports: dict[int, str] = {}
    found_selected: set[str] = set()
    for index, raw in enumerate(raw_inbounds):
        if not isinstance(raw, dict):
            raise MigrationError(f"inbounds[{index}] must be an object")
        tag = source_tag(raw, index)
        if options.selected_tags and tag not in options.selected_tags:
            continue
        found_selected.add(tag)
        try:
            converted = convert_one(raw, index, options, result.warnings)
        except UnsupportedInbound as exc:
            if options.strict:
                raise MigrationError(str(exc)) from exc
            result.skipped.append(str(exc))
            continue

        converted_tag = converted["tag"]
        port = int(converted["port"])
        if converted_tag in seen_tags:
            raise MigrationError(f"duplicate converted tag: {converted_tag}")
        if port in seen_ports:
            raise MigrationError(
                f"converted port collision: {converted_tag} and {seen_ports[port]} both use {port}"
            )
        seen_tags.add(converted_tag)
        seen_ports[port] = converted_tag
        result.inbounds.append(converted)

    missing = options.selected_tags - found_selected
    if missing:
        raise MigrationError(f"selected tag(s) not found: {', '.join(sorted(missing))}")
    if not result.inbounds:
        raise MigrationError("no supported inbounds were selected for conversion")
    return result


def merge_inbounds(
    xray_config: dict[str, Any], converted: Iterable[dict[str, Any]], replace_existing: bool
) -> dict[str, Any]:
    merged = copy.deepcopy(xray_config)
    existing = merged.get("inbounds", [])
    if not isinstance(existing, list):
        raise MigrationError("Xray config inbounds must be an array")

    existing_by_tag: dict[str, int] = {}
    for index, raw in enumerate(existing):
        if not isinstance(raw, dict):
            continue
        tag = nonempty_string(raw.get("tag"))
        if tag:
            if tag in existing_by_tag:
                raise MigrationError(f"Xray config already contains duplicate tag: {tag}")
            existing_by_tag[tag] = index

    converted_list = list(converted)
    replacing_tags = {item["tag"] for item in converted_list if item["tag"] in existing_by_tag}
    if replacing_tags and not replace_existing:
        raise MigrationError(
            "Xray config already contains tag(s): "
            + ", ".join(sorted(replacing_tags))
            + "; pass --replace-existing to replace them"
        )

    occupied_ports: dict[int, str] = {}
    for raw in existing:
        if not isinstance(raw, dict):
            continue
        tag = nonempty_string(raw.get("tag"))
        if tag in replacing_tags:
            continue
        try:
            port = parse_port(raw.get("port"), f"existing inbound {tag or '<untagged>'!r}")
        except MigrationError:
            continue
        occupied_ports[port] = tag or "<untagged>"

    for inbound in converted_list:
        port = int(inbound["port"])
        if port in occupied_ports:
            raise MigrationError(
                f"port {port} for {inbound['tag']} conflicts with existing inbound {occupied_ports[port]}"
            )

    for inbound in converted_list:
        tag = inbound["tag"]
        if tag in existing_by_tag:
            existing[existing_by_tag[tag]] = inbound
        else:
            existing.append(inbound)
    merged["inbounds"] = existing
    return merged


def parse_mapping(values: list[str], kind: str) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for raw in values:
        if "=" not in raw:
            raise MigrationError(f"invalid {kind} mapping {raw!r}; expected OLD=NEW")
        left, right = raw.split("=", 1)
        if kind == "port":
            result[parse_port(left, "port map source")] = parse_port(right, "port map target")
        else:
            left = left.strip()
            right = right.strip()
            if not left or not right:
                raise MigrationError(f"invalid {kind} mapping {raw!r}")
            result[left] = right
    return result


def make_options(args: argparse.Namespace) -> ConversionOptions:
    return ConversionOptions(
        strict=args.strict,
        selected_tags=set(args.tag or []),
        port_offset=args.port_offset,
        port_map=parse_mapping(args.port_map or [], "port"),
        tag_suffix=args.tag_suffix,
        reality_public_keys=parse_mapping(args.reality_public_key or [], "reality public key"),
        xray_bin=args.xray_bin,
        derive_reality_keys=not args.no_derive_reality_key,
    )


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def atomic_write(path: Path, data: str, mode: int | None = None, owner: tuple[int, int] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        if owner is not None and os.geteuid() == 0:
            os.chown(temp_path, owner[0], owner[1])
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def run_command(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MigrationError(f"command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MigrationError(f"command timed out: {' '.join(command)}") from exc


def validate_xray_config(config: dict[str, Any], xray_bin: str) -> None:
    binary = shutil.which(xray_bin) if os.path.sep not in xray_bin else xray_bin
    if not binary or not Path(binary).exists():
        raise MigrationError(f"xray binary not found: {xray_bin}")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json") as handle:
        handle.write(json_text(config))
        handle.flush()
        proc = run_command([binary, "run", "-test", "-config", handle.name], timeout=30)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise MigrationError(f"Xray config test failed: {detail}")


def service_action(service: str, action: str) -> None:
    proc = run_command(["systemctl", action, service], timeout=60)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise MigrationError(f"systemctl {action} {service} failed: {detail}")


def service_active(service: str) -> bool:
    return run_command(["systemctl", "is-active", "--quiet", service], timeout=10).returncode == 0


def listening_port_owners() -> dict[int, str]:
    if not shutil.which("ss"):
        return {}
    proc = run_command(["ss", "-H", "-lntup"], timeout=10)
    if proc.returncode != 0:
        return {}
    result: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        local = fields[4]
        match = re.search(r":(\d+)$", local)
        if not match:
            continue
        port = int(match.group(1))
        process_match = re.search(r'users:\(\("([^"]+)', line)
        result[port] = process_match.group(1) if process_match else "unknown"
    return result


def port_conflict_guidance(conflicts: dict[int, str]) -> str:
    detail = ", ".join(f"{port}({owner})" for port, owner in sorted(conflicts.items()))
    ports = "|".join(str(port) for port in sorted(conflicts))
    owners = {owner.lower() for owner in conflicts.values()}
    lines = [
        f"目标端口仍被旧进程占用：{detail}",
        "Xray 配置尚未写入，请按下面步骤处理：",
    ]
    if any(owner in {"sui", "s-ui"} or "s-ui" in owner for owner in owners):
        lines.extend(
            [
                "1. 保持当前 SSH 会话，另开一个 SSH 窗口连接服务器。",
                "2. 新版 S-UI 的 sing-box Core 内嵌在 s-ui 服务中，执行：systemctl stop s-ui",
                "   注意：S-UI 面板会暂时离线，但数据库和节点不会被删除。",
            ]
        )
    elif any("sing-box" in owner or "singbox" in owner for owner in owners):
        lines.extend(
            [
                "1. 先确认这是需要迁移的旧 sing-box：systemctl status sing-box --no-pager",
                "2. 确认后停止它：systemctl stop sing-box",
                "   如果不是 systemd 服务，请用下面的 ss 命令定位 PID 后停止对应进程。",
            ]
        )
    else:
        lines.extend(
            [
                "1. 先用下面的 ss 命令确认占用进程。",
                "2. 停止对应服务；不要直接结束不认识的系统进程。",
            ]
        )
    lines.extend(
        [
            f"3. 确认端口已释放（命令无输出才算成功）：ss -H -lntup | grep -E ':({ports})([[:space:]]|$)'",
            "4. 回到 s-x 菜单，重新选择 5 执行正式迁移。",
            "也可以重新选择 5，并在询问是否自动停止来源服务时输入 y。",
            "不要使用 --allow-active-port 绕过同端口检查。",
        ]
    )
    return "\n".join(lines)


def source_service_for_owner(owner: str) -> str | None:
    normalized = owner.strip().lower()
    if normalized in {"sui", "s-ui"} or "s-ui" in normalized:
        return "s-ui"
    if "sing-box" in normalized or "singbox" in normalized:
        return "sing-box"
    return None


def source_services_for_conflicts(conflicts: dict[int, str]) -> list[str]:
    services: set[str] = set()
    unknown: list[str] = []
    for port, owner in sorted(conflicts.items()):
        service = source_service_for_owner(owner)
        if service:
            services.add(service)
        else:
            unknown.append(f"{port}({owner})")
    if unknown:
        raise MigrationError(
            "refusing to stop unrecognized port owner(s) automatically: " + ", ".join(unknown)
        )
    return sorted(services)


def wait_for_ports_released(ports: set[int], timeout: int = 12) -> dict[int, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = listening_port_owners()
        remaining = {port: active[port] for port in ports if port in active}
        if not remaining:
            return {}
        time.sleep(0.5)
    active = listening_port_owners()
    return {port: active[port] for port in ports if port in active}


def wait_for_ports(ports: set[int], timeout: int = 12) -> set[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = set(listening_port_owners())
        missing = ports - found
        if not missing:
            return set()
        time.sleep(0.5)
    return ports - set(listening_port_owners())


def post_migration_guidance(*, sync_confirmed: bool, stopped_services: Iterable[str] = ()) -> str:
    lines = ["", "miaomiaowuX 后续操作："]
    if sync_confirmed:
        lines.extend(
            [
                "1. 主控已确认节点同步，打开「节点管理」找到迁移后的节点。",
                "2. 使用 TCPing 检查延迟，再用客户端验证连接。",
            ]
        )
    else:
        lines.extend(
            [
                "1. 打开「服务管理」，找到当前这台 Agent 服务器（例如 RN）。",
                "2. 点击「扫描远程服务」。",
                "3. 扫描完成后点击「接受 Agent 现状」。",
                "4. 打开「节点管理」确认节点出现，并使用 TCPing 检查延迟。",
            ]
        )
    stopped = sorted(set(stopped_services))
    if stopped:
        joined = ", ".join(stopped)
        lines.extend(
            [
                f"脚本已停止来源服务：{joined}。",
                "脚本没有修改其开机启动状态；确认迁移无误后，如不再使用旧 Core，请自行禁用对应服务，避免重启后再次占用端口。",
            ]
        )
    return "\n".join(lines)


def manual_admin_node_guidance(config_path: Path, tags: Iterable[str]) -> str:
    lines = [
        "",
        "管理员节点手动切换：",
        "REVOKE 只删除 Xray 中的原 S-UI client，不会修改 miaomiaowuX 节点管理。",
        "请在服务器本地运行下面的命令，第一列是 client 标签，第二列是对应凭据：",
    ]
    jq_filter = (
        '.inbounds[] | select(.tag == $tag) | .settings.clients[] | '
        '[(.email // .user // "<unlabeled>"), '
        '(.id // .password // .auth // .pass // "<no-credential>")] | @tsv'
    )
    for tag in sorted(set(tags)):
        command = (
            f"sudo jq -r --arg tag {shlex.quote(tag)} "
            f"{shlex.quote(jq_filter)} {shlex.quote(str(config_path))}"
        )
        lines.extend([f"- {tag}：", f"  {command}"])
    lines.extend(
        [
            "找到管理员 client（例如 黑西西）后，复制它对应的第二列值。",
            "在 miaomiaowuX「节点管理」中打开同 tag 节点的 Clash 配置详情：",
            "- VLESS/VMess：只把 uuid 改为管理员 client 的值。",
            "- Trojan/Hysteria：只把 password 改为管理员 client 的值。",
            "不要删除 Xray 入站，也不要删除后重新扫描节点；其他 server、port、Reality 和 tag 字段保持不变。",
            "保存后更新管理员订阅并做真实连接测试；套餐用户节点无需修改。",
        ]
    )
    return "\n".join(lines)


def wait_for_scan_result(started_at: dt.datetime, expected_count: int, timeout: int = 25) -> bool:
    log_path = Path("/var/log/mmw-agent/mmw-agent.log")
    deadline = time.monotonic() + timeout
    pattern = re.compile(r"Sent scan_result:.*inbounds=(\d+)")
    while time.monotonic() < deadline:
        text = ""
        if log_path.exists():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")[-200_000:]
            except OSError:
                text = ""
        for line in reversed(text.splitlines()):
            match = pattern.search(line)
            if not match:
                continue
            timestamp_match = re.match(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", line)
            if timestamp_match:
                try:
                    timestamp = dt.datetime.strptime(timestamp_match.group(1), "%Y/%m/%d %H:%M:%S")
                    if timestamp < started_at.replace(microsecond=0) - dt.timedelta(seconds=1):
                        continue
                except ValueError:
                    pass
            return int(match.group(1)) >= expected_count
        time.sleep(0.5)
    return False


def parse_yaml_scalar(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MigrationError(f"invalid double-quoted YAML scalar: {value}") from exc
        return parsed if isinstance(parsed, str) else str(parsed)
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise MigrationError(f"invalid single-quoted YAML scalar: {value}")
        return value[1:-1].replace("''", "'")
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def load_agent_connection(path: Path) -> tuple[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MigrationError(f"Agent config not found: {path}") from exc
    except OSError as exc:
        raise MigrationError(f"cannot read Agent config {path}: {exc}") from exc

    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        if not raw_line or raw_line[0].isspace() or raw_line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", raw_line)
        if match:
            values[match.group(1)] = parse_yaml_scalar(match.group(2))

    master_url = values.get("master_url", "").rstrip("/")
    token = values.get("token", "")
    parsed = urllib.parse.urlparse(master_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MigrationError(f"Agent config has an invalid master_url: {path}")
    if not token:
        raise MigrationError(f"Agent config has no token: {path}")
    return master_url, token


def request_master_node_sync(
    agent_config: Path,
    *,
    expected_tags: Iterable[str] = (),
    remove_absent_tags: Iterable[str] = (),
    timeout: int = 45,
) -> dict[str, Any]:
    master_url, token = load_agent_connection(agent_config)
    expected = sorted(set(expected_tags))
    remove_absent = sorted(set(remove_absent_tags))
    body = json.dumps(
        {"expected_tags": expected, "remove_absent_tags": remove_absent},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        master_url + "/api/remote/sync-nodes",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": AGENT_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        detail = exc.read(16_384).decode("utf-8", errors="replace").strip()
        if exc.code == 404:
            raise MasterSyncUnavailable(
                "the current master does not provide the Agent sync API "
                "/api/remote/sync-nodes"
            ) from exc
        raise MigrationError(f"master node sync returned HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise MigrationError(f"cannot reach master node sync API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise MigrationError("master node sync API timed out") from exc

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MigrationError("master node sync returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise MigrationError("master node sync returned a non-object response")
    errors = result.get("errors")
    detail = "; ".join(str(item) for item in errors) if isinstance(errors, list) else ""
    if result.get("success") is not True:
        missing = result.get("missing_tags")
        if isinstance(missing, list) and missing:
            detail = f"missing node tags: {', '.join(str(tag) for tag in missing)}"
        raise MigrationError("master node sync failed" + (f": {detail}" if detail else ""))

    node_tags = result.get("node_tags")
    if not isinstance(node_tags, list) or any(not isinstance(tag, str) for tag in node_tags):
        raise MigrationError("master node sync response has no valid node_tags")
    missing = sorted(set(expected) - set(node_tags))
    if missing:
        raise MigrationError("master did not persist expected node tag(s): " + ", ".join(missing))
    return result


def client_credential_fingerprint(protocol: str, client: Any) -> str:
    """Return a one-way identifier for a client credential without storing its secret."""
    if not isinstance(client, dict):
        return ""
    material: list[str] = [protocol.strip().lower()]
    for key in ("id", "password", "auth"):
        value = nonempty_string(client.get(key))
        if value:
            material.extend([key, value])
            break
    else:
        username = nonempty_string(client.get("user"))
        password = nonempty_string(client.get("pass"))
        if not username or not password:
            return ""
        material.extend(["user-pass", username, password])
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_credential_records(inbounds: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for inbound in inbounds:
        tag = nonempty_string(inbound.get("tag"))
        protocol = nonempty_string(inbound.get("protocol")).lower()
        settings = inbound.get("settings")
        if not tag or not protocol or not isinstance(settings, dict):
            continue
        for container in ("clients", "accounts"):
            entries = settings.get(container)
            if not isinstance(entries, list):
                continue
            fingerprints = sorted(
                {
                    fingerprint
                    for entry in entries
                    if (fingerprint := client_credential_fingerprint(protocol, entry))
                }
            )
            if fingerprints:
                records[tag] = {
                    "protocol": protocol,
                    "container": container,
                    "fingerprints": fingerprints,
                }
                break
    return records


def _client_display_label(client: Any) -> str:
    if not isinstance(client, dict):
        return "<unlabeled>"
    for key in ("email", "user", "name"):
        value = nonempty_string(client.get(key))
        if value:
            return value
    return "<unlabeled>"


def _summarize_client_labels(labels: Iterable[str], limit: int = 8) -> str:
    values = sorted(set(labels))
    if not values:
        return "无"
    visible = values[:limit]
    summary = "、".join(visible)
    if len(values) > limit:
        summary += f"，另有 {len(values) - limit} 个"
    return summary


def client_inventory_report(config: dict[str, Any], records: dict[str, Any]) -> str:
    """Describe migrated inbound clients without revealing their credentials."""
    inbounds = config.get("inbounds")
    if not isinstance(inbounds, list):
        return ""
    by_tag = {
        nonempty_string(inbound.get("tag")): inbound
        for inbound in inbounds
        if isinstance(inbound, dict) and nonempty_string(inbound.get("tag"))
    }
    lines = ["", "Xray client 现状（不显示 UUID/密码）："]
    reported = False
    for tag, raw_record in sorted(records.items()):
        if not isinstance(tag, str) or not isinstance(raw_record, dict):
            continue
        inbound = by_tag.get(tag)
        if not isinstance(inbound, dict):
            continue
        protocol = nonempty_string(raw_record.get("protocol")).lower()
        container = nonempty_string(raw_record.get("container"))
        raw_fingerprints = raw_record.get("fingerprints")
        if not isinstance(raw_fingerprints, list):
            continue
        fingerprints = {
            value
            for value in raw_fingerprints
            if isinstance(value, str) and value
        }
        settings = inbound.get("settings")
        if not isinstance(settings, dict) or not isinstance(settings.get(container), list):
            continue

        source_labels: list[str] = []
        package_users: list[str] = []
        other_labels: list[str] = []
        entries = settings[container]
        package_suffix = f"__{tag}"
        for entry in entries:
            label = _client_display_label(entry)
            fingerprint = client_credential_fingerprint(protocol, entry)
            if fingerprint and fingerprint in fingerprints:
                source_labels.append(label)
            elif label.endswith(package_suffix) and len(label) > len(package_suffix):
                package_users.append(label[: -len(package_suffix)])
            else:
                other_labels.append(label)

        remaining = len(entries) - len(source_labels)
        lines.extend(
            [
                f"- {tag}：共 {len(entries)} 个 client",
                f"  原 S-UI/sing-box：{len(source_labels)} 个",
                "  miaomiaowuX 套餐用户："
                f"{len(package_users)} 个（{_summarize_client_labels(package_users)}）",
                "  miaomiaowuX 管理员/其他："
                f"{len(other_labels)} 个（{_summarize_client_labels(other_labels)}）",
                f"  REVOKE 后预计保留：{remaining} 个",
            ]
        )
        reported = True
    return "\n".join(lines) if reported else ""


def recover_legacy_source_credential_records(
    state: dict[str, Any], xray_bin: str
) -> dict[str, dict[str, Any]]:
    """Rebuild fingerprints for a 0.4.0 state by re-reading its original source."""
    source_value = nonempty_string(state.get("source"))
    source_type = nonempty_string(state.get("source_type"))
    deployed_tags = {
        tag for tag in state.get("deployed_tags", []) if isinstance(tag, str) and tag
    }
    if not source_value or not deployed_tags:
        raise MigrationError(
            "the legacy migration state cannot identify its source and deployed tags; "
            "run a new migration with singbox-to-xray 0.4.1"
        )
    source_path = Path(source_value)
    if source_type == "s-ui-db":
        source_config = load_sui_database(source_path)
    elif source_type == "json":
        source_config = load_json(source_path)
    else:
        raise MigrationError(
            f"the legacy migration state has an unsupported source type: {source_type or '<empty>'}"
        )
    result = convert_config(
        source_config,
        ConversionOptions(
            strict=True,
            selected_tags=deployed_tags,
            xray_bin=xray_bin,
        ),
    )
    records = source_credential_records(result.inbounds)
    if not records:
        raise MigrationError(
            "no removable source client could be recovered from the legacy migration source; "
            "do not edit Xray manually"
        )
    return records


def remove_recorded_source_clients(
    config: dict[str, Any], records: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, int]]:
    candidate = copy.deepcopy(config)
    inbounds = candidate.get("inbounds")
    if not isinstance(inbounds, list):
        raise MigrationError("Xray config has no inbound list")
    by_tag = {
        nonempty_string(inbound.get("tag")): inbound
        for inbound in inbounds
        if isinstance(inbound, dict) and nonempty_string(inbound.get("tag"))
    }
    removed: dict[str, int] = {}
    for tag, raw_record in records.items():
        if not isinstance(tag, str) or not tag or not isinstance(raw_record, dict):
            raise MigrationError("migration state contains an invalid source credential record")
        protocol = nonempty_string(raw_record.get("protocol")).lower()
        container = nonempty_string(raw_record.get("container"))
        raw_fingerprints = raw_record.get("fingerprints")
        if container not in {"clients", "accounts"} or not isinstance(raw_fingerprints, list):
            raise MigrationError(f"migration state has an invalid credential record for {tag}")
        fingerprints = {
            value for value in raw_fingerprints if isinstance(value, str) and value
        }
        if not protocol or not fingerprints:
            raise MigrationError(f"migration state has no source credential fingerprints for {tag}")
        inbound = by_tag.get(tag)
        if not isinstance(inbound, dict):
            raise MigrationError(f"deployed inbound is missing from Xray config: {tag}")
        current_protocol = nonempty_string(inbound.get("protocol")).lower()
        if current_protocol != protocol:
            raise MigrationError(
                f"protocol for {tag} changed from {protocol} to {current_protocol or '<empty>'}"
            )
        settings = inbound.get("settings")
        if not isinstance(settings, dict) or not isinstance(settings.get(container), list):
            raise MigrationError(f"credential list {container} is missing from inbound {tag}")

        matching: list[Any] = []
        remaining: list[Any] = []
        remaining_credentials = 0
        for entry in settings[container]:
            fingerprint = client_credential_fingerprint(protocol, entry)
            if fingerprint and fingerprint in fingerprints:
                matching.append(entry)
            else:
                remaining.append(entry)
                if fingerprint:
                    remaining_credentials += 1
        if not matching:
            continue
        if remaining_credentials == 0:
            raise MigrationError(
                f"refusing to remove source clients from {tag}: no replacement Xray client "
                "was detected; scan remote services and accept Agent state first"
            )
        settings[container] = remaining
        removed[tag] = len(matching)
    return candidate, removed


def backup_config(config_path: Path) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = config_path.with_name(f".{config_path.name}.mmwx-migrate-{timestamp}.bak")
    try:
        shutil.copy2(config_path, backup)
        os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise MigrationError(f"failed to back up {config_path}: {exc}") from exc
    return backup


def write_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write(path, json_text(state), mode=stat.S_IRUSR | stat.S_IWUSR)


def restore_backup(config_path: Path, backup_path: Path, xray_service: str) -> None:
    if not backup_path.exists():
        raise MigrationError(f"backup file does not exist: {backup_path}")
    current_stat = config_path.stat() if config_path.exists() else backup_path.stat()
    data = backup_path.read_text(encoding="utf-8")
    atomic_write(
        config_path,
        data,
        mode=stat.S_IMODE(current_stat.st_mode),
        owner=(current_stat.st_uid, current_stat.st_gid),
    )
    service_action(xray_service, "restart")
    if not service_active(xray_service):
        raise MigrationError("backup restored, but Xray is still not active")


def report_conversion(result: ConversionResult) -> None:
    for inbound in result.inbounds:
        log("OK", f"converted {inbound['tag']}: {inbound['protocol']} port={inbound['port']}")
    for warning in result.warnings:
        log("WARN", warning)
    for skipped in result.skipped:
        log("SKIP", skipped)


def command_inspect(args: argparse.Namespace) -> int:
    source = resolve_source(args)
    inbounds = source.config.get("inbounds", [])
    print(f"\n可迁移入站（来源：{source.label}）：")
    for index, inbound in enumerate(inbounds, 1):
        if not isinstance(inbound, dict):
            print(f"  {index}. <invalid inbound>")
            continue
        inbound_type = nonempty_string(inbound.get("type")) or "<unknown>"
        tag = source_tag(inbound, index - 1)
        port = inbound.get("listen_port", "<unknown>")
        transport = inbound.get("transport")
        network = "tcp"
        if isinstance(transport, dict):
            network = nonempty_string(transport.get("type")) or "tcp"
        tls = inbound.get("tls")
        security = "none"
        if isinstance(tls, dict) and tls.get("enabled", True):
            reality = tls.get("reality")
            security = "reality" if isinstance(reality, dict) and reality.get("enabled", True) else "tls"
        users = inbound.get("users")
        user_count = len(users) if isinstance(users, list) else 0
        supported = "支持" if inbound_type.lower() in SUPPORTED_TYPES else "不支持"
        print(
            f"  {index}. {tag} | {inbound_type} | 端口 {port} | "
            f"{network} + {security} | 用户 {user_count} | {supported}"
        )
    return 0


def command_convert(args: argparse.Namespace) -> int:
    source = resolve_source(args)
    result = convert_config(source.config, make_options(args))
    report_conversion(result)
    payload: Any = result.inbounds if args.array else {"inbounds": result.inbounds}
    output = json_text(payload)
    if args.output == "-":
        sys.stdout.write(output)
    else:
        atomic_write(Path(args.output), output, mode=stat.S_IRUSR | stat.S_IWUSR)
        log("OK", f"wrote converted inbounds to {args.output}")
    return 0


def command_deploy(args: argparse.Namespace) -> int:
    source = resolve_source(args)
    config_path = Path(args.xray_config)
    current = load_json(config_path)
    result = convert_config(source.config, make_options(args))
    merged = merge_inbounds(current, result.inbounds, args.replace_existing)
    report_conversion(result)

    if not args.skip_xray_test:
        validate_xray_config(merged, args.xray_bin)
        log("OK", "merged Xray config passed xray -test")
    elif args.apply:
        raise MigrationError("--skip-xray-test is not allowed together with --apply")

    if args.output:
        atomic_write(Path(args.output), json_text(merged), mode=stat.S_IRUSR | stat.S_IWUSR)
        log("OK", f"wrote merged preview to {args.output}")

    if not args.apply:
        log("DRY-RUN", "configuration was not changed; pass --apply to deploy")
        return 0
    if os.geteuid() != 0:
        raise MigrationError("deploy --apply must run as root")

    desired_ports = {int(inbound["port"]) for inbound in result.inbounds}
    source_services: list[str] = []
    if not args.allow_active_port:
        active = listening_port_owners()
        conflicts = {port: active[port] for port in desired_ports if port in active and active[port] != "xray"}
        if conflicts:
            if not args.stop_source_services:
                raise MigrationError(port_conflict_guidance(conflicts))
            try:
                source_services = source_services_for_conflicts(conflicts)
            except MigrationError as exc:
                raise MigrationError(f"{exc}\n{port_conflict_guidance(conflicts)}") from exc

    config_stat = config_path.stat()
    backup = backup_config(config_path)
    stopped_source_services: list[str] = []
    state = {
        "version": 2,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": str(source.path),
        "source_type": source.kind,
        "xray_config": str(config_path),
        "backup": str(backup),
        "xray_bin": args.xray_bin,
        "xray_service": args.xray_service,
        "agent_service": args.agent_service,
        "agent_config": args.agent_config,
        "deployed_tags": [item["tag"] for item in result.inbounds],
        "deployed_ports": sorted(desired_ports),
        "source_credentials": source_credential_records(result.inbounds),
        "source_services_to_stop": source_services,
        "status": "deploying",
    }
    write_state(Path(args.state_file), state)

    try:
        for service in source_services:
            was_active = service_active(service)
            log("ACTION", f"stopping source service {service} to release migrated port(s)")
            if was_active:
                stopped_source_services.append(service)
            service_action(service, "stop")
        if source_services:
            remaining_conflicts = wait_for_ports_released(desired_ports)
            if remaining_conflicts:
                raise MigrationError(port_conflict_guidance(remaining_conflicts))
            state["stopped_source_services"] = stopped_source_services
            write_state(Path(args.state_file), state)
            log("OK", "source service stopped and target port(s) released")

        atomic_write(
            config_path,
            json_text(merged),
            mode=stat.S_IMODE(config_stat.st_mode),
            owner=(config_stat.st_uid, config_stat.st_gid),
        )
        service_action(args.xray_service, "restart")
        if not service_active(args.xray_service):
            raise MigrationError("Xray did not become active after restart")
        missing_ports = wait_for_ports(desired_ports)
        if missing_ports:
            raise MigrationError(
                "Xray is active but target port(s) are not listening: "
                + ", ".join(str(port) for port in sorted(missing_ports))
            )
    except Exception as exc:
        log("ERROR", f"deployment failed, restoring {backup}")
        recovery_errors: list[str] = []
        try:
            restore_backup(config_path, backup, args.xray_service)
            state["status"] = "auto_rolled_back"
            state["error"] = str(exc)
        except Exception as rollback_exc:
            recovery_errors.append(f"automatic Xray rollback failed: {rollback_exc}")
        for service in reversed(stopped_source_services):
            try:
                service_action(service, "start")
                log("OK", f"restarted source service {service} after deployment failure")
            except MigrationError as restart_exc:
                recovery_errors.append(f"failed to restart {service}: {restart_exc}")
        write_state(Path(args.state_file), state)
        if recovery_errors:
            raise MigrationError(f"deployment failed: {exc}; {'; '.join(recovery_errors)}") from exc
        raise MigrationError(f"deployment failed and was rolled back: {exc}") from exc

    state["status"] = "deployed"
    write_state(Path(args.state_file), state)
    log("OK", f"deployed {len(result.inbounds)} inbound(s); backup={backup}")

    if args.notify_master:
        started_at = dt.datetime.now()
        service_action(args.agent_service, "restart")
        if not service_active(args.agent_service):
            raise MigrationError("Xray is deployed, but mmw-agent did not become active")
        expected_count = len([item for item in merged.get("inbounds", []) if item.get("tag") != "api"])
        if not wait_for_scan_result(started_at, expected_count, args.scan_timeout):
            raise MigrationError(
                "Xray is deployed, but no matching Agent scan_result was observed; "
                "check mmw-agent connectivity and the master manually"
            )
        state["status"] = "reported"
        state["reported_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_state(Path(args.state_file), state)
        log("OK", f"Agent reported scan_result with at least {expected_count} managed inbound(s)")
        try:
            sync_result = request_master_node_sync(
                Path(args.agent_config),
                expected_tags=state["deployed_tags"],
                timeout=args.master_sync_timeout,
            )
        except MasterSyncUnavailable as exc:
            state["status"] = "manual_sync_required"
            state["master_sync_error"] = str(exc)
            write_state(Path(args.state_file), state)
            log("WARN", "Xray migration and Agent scan succeeded; this master requires manual acceptance")
            print(
                post_migration_guidance(
                    sync_confirmed=False, stopped_services=stopped_source_services
                ),
                file=sys.stderr,
            )
            return 0
        except MigrationError as exc:
            state["status"] = "master_sync_failed"
            state["master_sync_error"] = str(exc)
            write_state(Path(args.state_file), state)
            print(
                post_migration_guidance(
                    sync_confirmed=False, stopped_services=stopped_source_services
                ),
                file=sys.stderr,
            )
            raise MigrationError(f"Xray and Agent are healthy, but node sync was not confirmed: {exc}") from exc
        state["status"] = "synced"
        state["synced_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        state["master_server_id"] = sync_result.get("server_id")
        state["master_server_name"] = sync_result.get("server_name")
        state["node_tags"] = sync_result.get("node_tags", [])
        state.pop("master_sync_error", None)
        write_state(Path(args.state_file), state)
        log("OK", f"master confirmed node tag(s): {', '.join(state['deployed_tags'])}")
        print(
            post_migration_guidance(
                sync_confirmed=True, stopped_services=stopped_source_services
            ),
            file=sys.stderr,
        )
    else:
        state["status"] = "manual_sync_required"
        write_state(Path(args.state_file), state)
        print(
            post_migration_guidance(
                sync_confirmed=False, stopped_services=stopped_source_services
            ),
            file=sys.stderr,
        )
    return 0


def command_revoke_source_clients(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise MigrationError("revoke-source-clients must run as root")
    state_path = Path(args.state_file)
    state = load_json(state_path)
    xray_bin = args.xray_bin or nonempty_string(state.get("xray_bin")) or "xray"
    records = state.get("source_credentials")
    if not isinstance(records, dict) or not records:
        records = recover_legacy_source_credential_records(state, xray_bin)
        state["source_credentials"] = records
        state["source_credentials_recovered_from_legacy_state"] = True
        write_state(state_path, state)
        log("OK", "recovered source client fingerprints from the legacy migration source")
    config_value = args.xray_config or nonempty_string(state.get("xray_config"))
    if not config_value:
        raise MigrationError(f"invalid migration state: {state_path}")
    config_path = Path(config_value)
    current = load_json(config_path)
    inventory = client_inventory_report(current, records)
    if inventory:
        print(inventory, file=sys.stderr)
    candidate, removed = remove_recorded_source_clients(current, records)
    if not removed:
        log("OK", "recorded source clients are already absent; no configuration change was needed")
        return 0

    xray_service = args.xray_service or nonempty_string(state.get("xray_service")) or "xray"
    validate_xray_config(candidate, xray_bin)
    log("OK", "credential-pruned Xray config passed xray -test")

    config_stat = config_path.stat()
    backup = backup_config(config_path)
    affected_ports = {
        int(inbound["port"])
        for inbound in candidate.get("inbounds", [])
        if isinstance(inbound, dict)
        and nonempty_string(inbound.get("tag")) in removed
        and isinstance(inbound.get("port"), int)
    }
    state["source_client_revoke_backup"] = str(backup)
    state["source_client_revoke_backup_restores_legacy_credentials"] = True
    state["status"] = "revoking_source_clients"
    write_state(state_path, state)

    try:
        atomic_write(
            config_path,
            json_text(candidate),
            mode=stat.S_IMODE(config_stat.st_mode),
            owner=(config_stat.st_uid, config_stat.st_gid),
        )
        service_action(xray_service, "restart")
        if not service_active(xray_service):
            raise MigrationError("Xray did not become active after source client revocation")
        missing_ports = wait_for_ports(affected_ports)
        if missing_ports:
            raise MigrationError(
                "Xray is active but revoked inbound port(s) are not listening: "
                + ", ".join(str(port) for port in sorted(missing_ports))
            )
    except Exception as exc:
        log("ERROR", f"source client revocation failed, restoring {backup}")
        try:
            restore_backup(config_path, backup, xray_service)
        except Exception as rollback_exc:
            state["status"] = "source_client_revoke_rollback_failed"
            state["source_client_revoke_error"] = str(exc)
            state["source_client_revoke_rollback_error"] = str(rollback_exc)
            write_state(state_path, state)
            raise MigrationError(
                f"source client revocation failed: {exc}; automatic rollback failed: {rollback_exc}"
            ) from exc
        state["status"] = "source_client_revoke_auto_rolled_back"
        state["source_client_revoke_error"] = str(exc)
        write_state(state_path, state)
        raise MigrationError(f"source client revocation failed and was rolled back: {exc}") from exc

    state["status"] = "source_clients_revoked"
    state["source_clients_revoked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    state["source_clients_revoked"] = removed
    state.pop("source_client_revoke_error", None)
    state.pop("source_client_revoke_rollback_error", None)
    write_state(state_path, state)
    for tag, count in sorted(removed.items()):
        log("OK", f"revoked {count} recorded source client(s) from {tag}")
    log("WARN", f"backup {backup} still contains the revoked credentials")
    print(
        "原 S-UI client 已从当前 Xray 配置删除。\n"
        "原 S-UI 节点应失败；用户套餐节点应保持正常。",
        file=sys.stderr,
    )
    print(
        manual_admin_node_guidance(config_path, removed),
        file=sys.stderr,
    )
    return 0


def command_rollback(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise MigrationError("rollback must run as root")
    state_path = Path(args.state_file)
    state = load_json(state_path)
    config_value = nonempty_string(state.get("xray_config"))
    backup_value = nonempty_string(state.get("backup"))
    if not config_value or not backup_value:
        raise MigrationError(f"invalid migration state: {state_path}")
    config_path = Path(config_value)
    backup_path = Path(backup_value)
    xray_service = args.xray_service or state.get("xray_service", "xray")
    agent_service = args.agent_service or state.get("agent_service", "mmw-agent")
    agent_config = args.agent_config or state.get("agent_config", DEFAULT_AGENT_CONFIG)
    deployed_tags = [tag for tag in state.get("deployed_tags", []) if isinstance(tag, str) and tag]
    stopped_source_services = [
        service
        for service in state.get("stopped_source_services", [])
        if service in {"s-ui", "sing-box"}
    ]
    restore_backup(config_path, backup_path, xray_service)
    for service in stopped_source_services:
        try:
            service_action(service, "start")
        except MigrationError as exc:
            state["status"] = "rolled_back_source_restart_failed"
            state["rollback_error"] = str(exc)
            write_state(state_path, state)
            raise MigrationError(
                f"Xray backup was restored, but source service {service} could not be restarted: {exc}"
            ) from exc
        log("OK", f"restarted source service {service} after rollback")
    state["status"] = "rolled_back"
    state["rolled_back_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    state["restarted_source_services"] = stopped_source_services
    write_state(state_path, state)
    log("OK", f"restored Xray config from {backup_path}")
    if args.notify_master:
        started_at = dt.datetime.now()
        service_action(agent_service, "restart")
        if not service_active(agent_service):
            raise MigrationError("rollback succeeded, but mmw-agent did not become active")
        restored = load_json(config_path)
        expected_count = len([item for item in restored.get("inbounds", []) if item.get("tag") != "api"])
        if not wait_for_scan_result(started_at, expected_count, args.scan_timeout):
            raise MigrationError("rollback succeeded, but Agent scan_result was not observed")
        log("OK", "Agent reported the restored Xray configuration")
        restored_tags = [
            item.get("tag")
            for item in restored.get("inbounds", [])
            if isinstance(item, dict) and item.get("tag") and item.get("tag") != "api"
        ]
        try:
            sync_result = request_master_node_sync(
                Path(agent_config),
                expected_tags=restored_tags,
                remove_absent_tags=deployed_tags,
                timeout=args.master_sync_timeout,
            )
        except MigrationError as exc:
            state["status"] = "rolled_back_master_sync_failed"
            state["master_sync_error"] = str(exc)
            write_state(state_path, state)
            raise MigrationError(f"rollback succeeded, but master node cleanup was not confirmed: {exc}") from exc
        node_tags = set(sync_result.get("node_tags", []))
        stale = sorted((set(deployed_tags) - set(restored_tags)) & node_tags)
        if stale:
            state["status"] = "rolled_back_master_sync_failed"
            state["master_sync_error"] = "stale node tags: " + ", ".join(stale)
            write_state(state_path, state)
            raise MigrationError("rollback succeeded, but stale master node tag(s) remain: " + ", ".join(stale))
        state["status"] = "rolled_back_synced"
        state["master_synced_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        state["node_tags"] = sync_result.get("node_tags", [])
        state.pop("master_sync_error", None)
        write_state(state_path, state)
        log("OK", "master confirmed the restored node set")
    return 0


def menu_prompt(message: str) -> str:
    print(message, end="", flush=True)
    value = sys.stdin.readline()
    if value == "":
        raise MigrationError("interactive menu was closed")
    return value.strip()


def run_menu_action(argv: list[str]) -> int:
    print()
    exit_code = main(argv)
    if exit_code == 0:
        print("\n操作完成。")
    else:
        print(f"\n操作失败，退出码：{exit_code}")
    return exit_code


def command_menu(_args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        raise MigrationError("the interactive menu requires a terminal")
    try:
        while True:
            print(
                f"""
singbox_to_xray {VERSION}
========================================
  1. 查看数据源与可迁移入站
  2. 安全预检（推荐，不写入）
  3. 选择数据源后预检
  4. 隔离端口预检
  5. 正式迁移到 Xray（可自动停止旧 Core）
  6. 删除原 S-UI client（需先确认新节点正常）
  7. 回滚最近一次迁移
  8. 显示命令帮助
  0. 退出
========================================"""
            )
            choice = menu_prompt("请选择 [0-8]：")
            if choice == "0":
                print("已退出。")
                return 0
            if choice == "1":
                run_menu_action(["inspect", "--interactive"])
            elif choice == "2":
                run_menu_action(["deploy", "--strict"])
            elif choice == "3":
                run_menu_action(["deploy", "--interactive", "--strict"])
            elif choice == "4":
                offset = menu_prompt("端口偏移量 [10000]：") or "10000"
                try:
                    int(offset)
                except ValueError:
                    print("端口偏移量必须是整数。")
                    continue
                suffix = menu_prompt("tag 后缀 [-stage]：") or "-stage"
                run_menu_action(
                    [
                        "deploy",
                        "--interactive",
                        "--strict",
                        "--port-offset",
                        offset,
                        f"--tag-suffix={suffix}",
                    ]
                )
            elif choice == "5":
                print(
                    "\n正式迁移会备份并写入 Xray 配置，然后重启 Xray。\n"
                    "如果旧 Core 占用目标端口，脚本可以在确认后停止对应服务。"
                )
                if menu_prompt("输入 APPLY 继续，其他内容取消：") != "APPLY":
                    print("已取消正式迁移。")
                    continue
                command = ["deploy", "--interactive", "--strict", "--apply"]
                if menu_prompt(
                    "端口被 s-ui/sing-box 占用时，由脚本自动停止对应服务？[y/N]："
                ).lower() in {"y", "yes"}:
                    command.append("--stop-source-services")
                if menu_prompt("替换 Xray 中同 tag 入站？[y/N]：").lower() in {"y", "yes"}:
                    command.append("--replace-existing")
                if menu_prompt(
                    "尝试自动通知 miaomiaowuX 主控？不支持时会提示手动同步。[y/N]："
                ).lower() in {"y", "yes"}:
                    command.append("--notify-master")
                run_menu_action(command)
            elif choice == "6":
                print(
                    "\n此操作只删除迁移时记录的原 S-UI client，不修改节点管理和主控数据。\n"
                    "请先完成‘扫描远程服务 → 接受 Agent 现状’，并确认管理员节点和用户套餐节点真实可用。"
                )
                if menu_prompt("输入 REVOKE 继续，其他内容取消：") != "REVOKE":
                    print("已取消删除原 S-UI client。")
                    continue
                run_menu_action(["revoke-source-clients"])
            elif choice == "7":
                print("\n回滚会恢复最近一次部署前的 Xray 配置并重启 Xray。")
                if menu_prompt("输入 ROLLBACK 继续，其他内容取消：") != "ROLLBACK":
                    print("已取消回滚。")
                    continue
                command = ["rollback"]
                if menu_prompt("回滚后通知 miaomiaowuX 主控？[y/N]：").lower() in {"y", "yes"}:
                    command.append("--notify-master")
                run_menu_action(command)
            elif choice == "8":
                print()
                build_parser().print_help()
            else:
                print("无效选项，请重新选择。")
    except KeyboardInterrupt:
        print("\n已取消并退出。")
        return 130


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--input",
        help=(
            "sing-box config.json path; when omitted, prefer a detected S-UI database "
            "and fall back to the default JSON path"
        ),
    )
    source_group.add_argument(
        "--s-ui-db",
        metavar="PATH",
        help=f"read S-UI 1.5.x inbounds from SQLite (default auto-detect: {DEFAULT_SUI_DB})",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="ask which source to use when multiple non-empty sources are detected",
    )


def add_conversion_arguments(parser: argparse.ArgumentParser) -> None:
    add_source_arguments(parser)
    parser.add_argument("--tag", action="append", help="convert only this source inbound tag; repeatable")
    parser.add_argument("--strict", action="store_true", help="fail instead of skipping unsupported inbounds")
    parser.add_argument("--port-offset", type=int, default=0, help="add this value to every source port")
    parser.add_argument("--port-map", action="append", default=[], metavar="OLD=NEW", help="explicit port mapping")
    parser.add_argument("--tag-suffix", default="", help="suffix added to converted tags, e.g. -stage")
    parser.add_argument(
        "--reality-public-key",
        action="append",
        default=[],
        metavar="TAG=KEY",
        help="REALITY public key override",
    )
    parser.add_argument("--xray-bin", default="xray", help="Xray executable path or command")
    parser.add_argument(
        "--no-derive-reality-key",
        action="store_true",
        help="do not derive a REALITY public key with xray x25519",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert S-UI/sing-box inbounds to miaomiaowuX-managed Xray inbounds"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    menu_parser = subparsers.add_parser("menu", help="open the interactive Chinese menu")
    menu_parser.set_defaults(handler=command_menu)

    inspect_parser = subparsers.add_parser("inspect", help="list source inbounds without secrets")
    add_source_arguments(inspect_parser)
    inspect_parser.set_defaults(handler=command_inspect)

    convert_parser = subparsers.add_parser("convert", help="convert only; never modify Xray")
    add_conversion_arguments(convert_parser)
    convert_parser.add_argument("--output", "-o", default="-", help="output path, or - for stdout")
    convert_parser.add_argument("--array", action="store_true", help="output a raw inbound array")
    convert_parser.set_defaults(handler=command_convert)

    deploy_parser = subparsers.add_parser("deploy", help="merge into Xray, validate, and optionally apply")
    add_conversion_arguments(deploy_parser)
    deploy_parser.add_argument("--xray-config", default=DEFAULT_XRAY_CONFIG)
    deploy_parser.add_argument("--replace-existing", action="store_true")
    deploy_parser.add_argument("--output", help="write the merged preview to this path")
    deploy_parser.add_argument("--skip-xray-test", action="store_true", help="preview only; forbidden with --apply")
    deploy_parser.add_argument("--apply", action="store_true", help="write and restart Xray")
    deploy_parser.add_argument(
        "--stop-source-services",
        action="store_true",
        help="stop recognized s-ui/sing-box services when they own migrated ports",
    )
    deploy_parser.add_argument(
        "--notify-master",
        action="store_true",
        help="restart Agent, request master node sync, and verify persisted node tags",
    )
    deploy_parser.add_argument(
        "--allow-active-port",
        action="store_true",
        help="advanced override for non-Xray port ownership; unsafe for same-port migration",
    )
    deploy_parser.add_argument("--xray-service", default="xray")
    deploy_parser.add_argument("--agent-service", default="mmw-agent")
    deploy_parser.add_argument("--agent-config", default=DEFAULT_AGENT_CONFIG)
    deploy_parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    deploy_parser.add_argument("--scan-timeout", type=int, default=25)
    deploy_parser.add_argument("--master-sync-timeout", type=int, default=45)
    deploy_parser.set_defaults(handler=command_deploy)

    revoke_parser = subparsers.add_parser(
        "revoke-source-clients",
        help="remove the source S-UI clients recorded during the latest migration",
    )
    revoke_parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    revoke_parser.add_argument("--xray-config")
    revoke_parser.add_argument("--xray-bin")
    revoke_parser.add_argument("--xray-service")
    revoke_parser.set_defaults(handler=command_revoke_source_clients)

    rollback_parser = subparsers.add_parser("rollback", help="restore the latest deployment backup")
    rollback_parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    rollback_parser.add_argument("--notify-master", action="store_true")
    rollback_parser.add_argument("--xray-service")
    rollback_parser.add_argument("--agent-service")
    rollback_parser.add_argument("--agent-config")
    rollback_parser.add_argument("--scan-timeout", type=int, default=25)
    rollback_parser.add_argument("--master-sync-timeout", type=int, default=45)
    rollback_parser.set_defaults(handler=command_rollback)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    effective_argv = sys.argv[1:] if argv is None else argv
    if not effective_argv and sys.stdin.isatty():
        effective_argv = ["menu"]
    args = parser.parse_args(effective_argv)
    if getattr(args, "notify_master", False) and not getattr(args, "apply", True):
        parser.error("--notify-master requires --apply")
    try:
        return int(args.handler(args))
    except MigrationError as exc:
        log("ERROR", str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
