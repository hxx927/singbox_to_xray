import argparse
import contextlib
import copy
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import singbox_to_xray as converter


class ConverterTests(unittest.TestCase):
    def options(self, **overrides):
        values = {
            "strict": True,
            "derive_reality_keys": False,
        }
        values.update(overrides)
        return converter.ConversionOptions(**values)

    def create_sui_database(self, path: Path, *, transport=None):
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE inbounds (
                id INTEGER PRIMARY KEY,
                type TEXT,
                tag TEXT,
                tls_id INTEGER,
                options TEXT
            );
            CREATE TABLE tls (
                id INTEGER PRIMARY KEY,
                server TEXT
            );
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY,
                enable INTEGER,
                config TEXT,
                inbounds TEXT
            );
            """
        )
        options = {"listen": "::", "listen_port": 443}
        if transport is not None:
            options["transport"] = transport
        tls = {
            "enabled": True,
            "server_name": "www.example.com",
            "reality": {
                "enabled": True,
                "handshake": {"server": "www.example.com", "server_port": 443},
                "private_key": "private-key",
                "short_id": ["0123456789abcdef"],
            },
        }
        client = {
            "vless": {
                "name": "alice",
                "uuid": "11111111-2222-3333-4444-555555555555",
                "flow": "xtls-rprx-vision",
            }
        }
        connection.execute("INSERT INTO tls(id, server) VALUES(?, ?)", (7, json.dumps(tls)))
        connection.execute(
            "INSERT INTO inbounds(id, type, tag, tls_id, options) VALUES(?, ?, ?, ?, ?)",
            (1, "vless", "vless-reality", 7, json.dumps(options)),
        )
        connection.execute(
            "INSERT INTO clients(id, enable, config, inbounds) VALUES(?, ?, ?, ?)",
            (1, 1, json.dumps(client), json.dumps([1])),
        )
        connection.execute(
            "INSERT INTO clients(id, enable, config, inbounds) VALUES(?, ?, ?, ?)",
            (2, 0, json.dumps(client), json.dumps([1])),
        )
        connection.commit()
        connection.close()

    def test_loads_sui_database_and_converts_dynamic_vless(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "s-ui.db"
            self.create_sui_database(database)

            source = converter.load_sui_database(database)

        self.assertEqual(len(source["inbounds"]), 1)
        inbound = source["inbounds"][0]
        self.assertEqual(inbound["tag"], "vless-reality")
        self.assertEqual(inbound["listen_port"], 443)
        self.assertEqual(len(inbound["users"]), 1)
        self.assertEqual(inbound["users"][0]["name"], "alice")
        self.assertEqual(inbound["users"][0]["flow"], "xtls-rprx-vision")
        with mock.patch.object(
            converter, "derive_reality_public_key", return_value="derived-public-key"
        ):
            converted = converter.convert_config(
                source, self.options(derive_reality_keys=True)
            ).inbounds[0]
        self.assertEqual(converted["protocol"], "vless")
        self.assertEqual(converted["streamSettings"]["security"], "reality")

    def test_sui_database_matches_vless_transport_flow_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "s-ui.db"
            self.create_sui_database(database, transport={"type": "ws", "path": "/ws"})

            source = converter.load_sui_database(database)

        self.assertEqual(source["inbounds"][0]["users"][0]["flow"], "")

    def test_auto_source_prefers_nonempty_sui_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "s-ui.db"
            config = root / "config.json"
            self.create_sui_database(database)
            config.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                "type": "shadowsocks",
                                "tag": "legacy-json",
                                "listen_port": 12345,
                                "method": "aes-128-gcm",
                                "password": "secret",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(input=None, s_ui_db=None, interactive=False)
            with mock.patch.object(converter, "DEFAULT_SUI_DB", str(database)), mock.patch.object(
                converter, "DEFAULT_SINGBOX_CONFIG", str(config)
            ):
                selected = converter.resolve_source(args)

        self.assertEqual(selected.kind, "s-ui-db")
        self.assertEqual(selected.config["inbounds"][0]["tag"], "vless-reality")

    def test_interactive_source_selection_can_choose_json(self):
        candidates = [
            converter.SourceDocument({"inbounds": [{}]}, "s-ui-db", Path("s-ui.db")),
            converter.SourceDocument({"inbounds": [{}]}, "json", Path("config.json")),
        ]
        with mock.patch.object(converter.sys.stdin, "isatty", return_value=True), mock.patch.object(
            converter.sys.stdin, "readline", return_value="2\n"
        ):
            selected = converter.choose_source(candidates, interactive=True)

        self.assertEqual(selected.kind, "json")

    def test_inspect_lists_metadata_without_credentials(self):
        source = converter.SourceDocument(
            {
                "inbounds": [
                    {
                        "type": "vless",
                        "tag": "vless-main",
                        "listen_port": 443,
                        "users": [
                            {
                                "uuid": "11111111-2222-3333-4444-555555555555",
                                "password": "secret-password",
                            }
                        ],
                        "tls": {
                            "enabled": True,
                            "reality": {"enabled": True, "private_key": "secret-key"},
                        },
                    }
                ]
            },
            "s-ui-db",
            Path("s-ui.db"),
        )
        output = io.StringIO()
        with mock.patch.object(converter, "resolve_source", return_value=source), contextlib.redirect_stdout(
            output
        ):
            exit_code = converter.command_inspect(argparse.Namespace())

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("vless-main", rendered)
        self.assertIn("reality", rendered)
        self.assertNotIn("11111111", rendered)
        self.assertNotIn("secret-password", rendered)
        self.assertNotIn("secret-key", rendered)

    def test_menu_safe_preflight_and_exit(self):
        output = io.StringIO()
        with mock.patch.object(converter.sys.stdin, "isatty", return_value=True), mock.patch.object(
            converter.sys.stdin, "readline", side_effect=["2\n", "0\n"]
        ), mock.patch.object(converter, "run_menu_action", return_value=0) as action, contextlib.redirect_stdout(
            output
        ):
            exit_code = converter.command_menu(argparse.Namespace())

        self.assertEqual(exit_code, 0)
        action.assert_called_once_with(["deploy", "--strict"])
        self.assertIn("安全预检", output.getvalue())

    def test_menu_cancelled_apply_does_not_run_action(self):
        with mock.patch.object(converter.sys.stdin, "isatty", return_value=True), mock.patch.object(
            converter.sys.stdin, "readline", side_effect=["5\n", "cancel\n", "0\n"]
        ), mock.patch.object(converter, "run_menu_action") as action, contextlib.redirect_stdout(
            io.StringIO()
        ):
            exit_code = converter.command_menu(argparse.Namespace())

        self.assertEqual(exit_code, 0)
        action.assert_not_called()

    def test_menu_apply_can_stop_source_services(self):
        with mock.patch.object(converter.sys.stdin, "isatty", return_value=True), mock.patch.object(
            converter.sys.stdin,
            "readline",
            side_effect=["5\n", "APPLY\n", "y\n", "n\n", "n\n", "0\n"],
        ), mock.patch.object(converter, "run_menu_action", return_value=0) as action, contextlib.redirect_stdout(
            io.StringIO()
        ):
            exit_code = converter.command_menu(argparse.Namespace())

        self.assertEqual(exit_code, 0)
        action.assert_called_once_with(
            ["deploy", "--interactive", "--strict", "--apply", "--stop-source-services"]
        )

    def test_menu_can_revoke_source_clients(self):
        with mock.patch.object(converter.sys.stdin, "isatty", return_value=True), mock.patch.object(
            converter.sys.stdin, "readline", side_effect=["6\n", "REVOKE\n", "0\n"]
        ), mock.patch.object(converter, "run_menu_action", return_value=0) as action, contextlib.redirect_stdout(
            io.StringIO()
        ):
            exit_code = converter.command_menu(argparse.Namespace())

        self.assertEqual(exit_code, 0)
        action.assert_called_once_with(["revoke-source-clients"])

    def test_port_conflict_guidance_for_embedded_sui(self):
        message = converter.port_conflict_guidance({50965: "sui"})

        self.assertIn("50965(sui)", message)
        self.assertIn("systemctl stop s-ui", message)
        self.assertIn("面板会暂时离线", message)
        self.assertIn("重新选择 5", message)
        self.assertIn("自动停止来源服务", message)
        self.assertIn("不要使用 --allow-active-port", message)

    def test_port_conflict_guidance_for_standalone_singbox(self):
        message = converter.port_conflict_guidance({443: "sing-box"})

        self.assertIn("systemctl status sing-box", message)
        self.assertIn("systemctl stop sing-box", message)
        self.assertIn(":(443)([[:space:]]|$)", message)

    def test_source_services_only_accept_known_port_owners(self):
        self.assertEqual(
            converter.source_services_for_conflicts({443: "sui", 8443: "sing-box"}),
            ["s-ui", "sing-box"],
        )
        with self.assertRaisesRegex(converter.MigrationError, "unrecognized"):
            converter.source_services_for_conflicts({443: "nginx"})

    def test_post_migration_guidance_describes_manual_acceptance(self):
        message = converter.post_migration_guidance(
            sync_confirmed=False, stopped_services=["s-ui"]
        )

        self.assertIn("服务管理", message)
        self.assertIn("扫描远程服务", message)
        self.assertIn("接受 Agent 现状", message)
        self.assertIn("节点管理", message)
        self.assertIn("TCPing", message)
        self.assertIn("没有修改其开机启动状态", message)

    def test_manual_admin_node_guidance_prints_safe_jq_command(self):
        message = converter.manual_admin_node_guidance(
            Path("/usr/local/etc/xray/config.json"), ["vless-50965"]
        )

        self.assertIn("jq -r --arg tag vless-50965", message)
        self.assertIn("黑西西", message)
        self.assertIn("只把 uuid", message)
        self.assertIn("不要删除 Xray 入站", message)
        self.assertIn("套餐用户节点无需修改", message)

    def test_deploy_stops_sui_and_prints_manual_sync_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sing-box.json"
            xray_config = root / "xray.json"
            state_file = root / "state.json"
            source.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                "type": "shadowsocks",
                                "tag": "ss-main",
                                "listen_port": 12315,
                                "method": "aes-128-gcm",
                                "password": "secret",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            xray_config.write_text('{"inbounds": []}\n', encoding="utf-8")
            args = converter.build_parser().parse_args(
                [
                    "deploy",
                    "--input",
                    str(source),
                    "--xray-config",
                    str(xray_config),
                    "--state-file",
                    str(state_file),
                    "--strict",
                    "--apply",
                    "--stop-source-services",
                ]
            )
            stderr = io.StringIO()
            with mock.patch.object(converter, "validate_xray_config"), mock.patch.object(
                converter.os, "geteuid", return_value=0
            ), mock.patch.object(converter.os, "chown"), mock.patch.object(
                converter, "listening_port_owners", side_effect=[{12315: "sui"}, {}]
            ), mock.patch.object(converter, "service_active", return_value=True), mock.patch.object(
                converter, "service_action"
            ) as service_action, mock.patch.object(
                converter, "wait_for_ports", return_value=set()
            ), contextlib.redirect_stderr(stderr):
                exit_code = converter.command_deploy(args)

            state = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(state["status"], "manual_sync_required")
        self.assertEqual(state["stopped_source_services"], ["s-ui"])
        service_action.assert_any_call("s-ui", "stop")
        service_action.assert_any_call("xray", "restart")
        self.assertNotIn(mock.call("s-ui", "start"), service_action.call_args_list)
        self.assertIn("接受 Agent 现状", stderr.getvalue())

    def test_deploy_restarts_stopped_source_service_after_xray_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sing-box.json"
            xray_config = root / "xray.json"
            state_file = root / "state.json"
            original = '{"inbounds": []}\n'
            source.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                "type": "shadowsocks",
                                "tag": "ss-main",
                                "listen_port": 12315,
                                "method": "aes-128-gcm",
                                "password": "secret",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            xray_config.write_text(original, encoding="utf-8")
            args = converter.build_parser().parse_args(
                [
                    "deploy",
                    "--input",
                    str(source),
                    "--xray-config",
                    str(xray_config),
                    "--state-file",
                    str(state_file),
                    "--strict",
                    "--apply",
                    "--stop-source-services",
                ]
            )
            calls: list[tuple[str, str]] = []
            xray_restarts = 0

            def service_action(service, action):
                nonlocal xray_restarts
                calls.append((service, action))
                if service == "xray" and action == "restart":
                    xray_restarts += 1
                    if xray_restarts == 1:
                        raise converter.MigrationError("simulated Xray restart failure")

            with mock.patch.object(converter, "validate_xray_config"), mock.patch.object(
                converter.os, "geteuid", return_value=0
            ), mock.patch.object(converter.os, "chown"), mock.patch.object(
                converter, "listening_port_owners", side_effect=[{12315: "sui"}, {}]
            ), mock.patch.object(converter, "service_active", return_value=True), mock.patch.object(
                converter, "service_action", side_effect=service_action
            ), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(converter.MigrationError, "rolled back"):
                    converter.command_deploy(args)

            state = json.loads(state_file.read_text(encoding="utf-8"))
            restored = xray_config.read_text(encoding="utf-8")

        self.assertEqual(state["status"], "auto_rolled_back")
        self.assertEqual(restored, original)
        self.assertIn(("s-ui", "stop"), calls)
        self.assertIn(("s-ui", "start"), calls)

    def test_deploy_treats_missing_master_api_as_manual_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sing-box.json"
            xray_config = root / "xray.json"
            state_file = root / "state.json"
            source.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                "type": "shadowsocks",
                                "tag": "ss-main",
                                "listen_port": 12315,
                                "method": "aes-128-gcm",
                                "password": "secret",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            xray_config.write_text('{"inbounds": []}\n', encoding="utf-8")
            args = converter.build_parser().parse_args(
                [
                    "deploy",
                    "--input",
                    str(source),
                    "--xray-config",
                    str(xray_config),
                    "--state-file",
                    str(state_file),
                    "--strict",
                    "--apply",
                    "--notify-master",
                ]
            )
            stderr = io.StringIO()
            with mock.patch.object(converter, "validate_xray_config"), mock.patch.object(
                converter.os, "geteuid", return_value=0
            ), mock.patch.object(converter.os, "chown"), mock.patch.object(
                converter, "listening_port_owners", return_value={}
            ), mock.patch.object(converter, "service_active", return_value=True), mock.patch.object(
                converter, "service_action"
            ), mock.patch.object(converter, "wait_for_ports", return_value=set()), mock.patch.object(
                converter, "wait_for_scan_result", return_value=True
            ), mock.patch.object(
                converter,
                "request_master_node_sync",
                side_effect=converter.MasterSyncUnavailable("missing Agent sync API"),
            ), contextlib.redirect_stderr(stderr):
                exit_code = converter.command_deploy(args)

            state = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(state["status"], "manual_sync_required")
        self.assertIn("requires manual acceptance", stderr.getvalue())
        self.assertIn("接受 Agent 现状", stderr.getvalue())

    def test_deploy_records_source_client_fingerprints_without_secrets(self):
        inbound = {
            "tag": "vless-main",
            "protocol": "vless",
            "settings": {
                "clients": [
                    {"id": "old-sui-uuid", "email": "sui-user"},
                ]
            },
        }

        records = converter.source_credential_records([inbound])

        serialized = json.dumps(records)
        self.assertIn("vless-main", records)
        self.assertNotIn("old-sui-uuid", serialized)
        self.assertEqual(records["vless-main"]["container"], "clients")
        self.assertEqual(len(records["vless-main"]["fingerprints"]), 1)

    def test_recovers_source_fingerprints_for_040_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "sing-box.json"
            source_path.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                "type": "vless",
                                "tag": "vless-main",
                                "listen_port": 443,
                                "users": [{"uuid": "old-sui-uuid"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            records = converter.recover_legacy_source_credential_records(
                {
                    "source": str(source_path),
                    "source_type": "json",
                    "deployed_tags": ["vless-main"],
                },
                "xray",
            )

        self.assertEqual(records["vless-main"]["container"], "clients")
        self.assertEqual(len(records["vless-main"]["fingerprints"]), 1)

    def test_revoke_source_clients_removes_only_recorded_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            xray_config = root / "xray.json"
            state_file = root / "state.json"
            current = {
                "inbounds": [
                    {
                        "tag": "vless-main",
                        "protocol": "vless",
                        "port": 443,
                        "settings": {
                            "clients": [
                                {"id": "old-sui-uuid", "email": "sui-user"},
                                {"id": "mmwx-admin-uuid", "email": "admin"},
                                {"id": "package-uuid", "email": "alice__vless-main"},
                            ]
                        },
                    }
                ]
            }
            xray_config.write_text(json.dumps(current), encoding="utf-8")
            state_file.write_text(
                json.dumps(
                    {
                        "xray_config": str(xray_config),
                        "xray_bin": "xray",
                        "xray_service": "xray",
                        "source_credentials": converter.source_credential_records(
                            [
                                {
                                    "tag": "vless-main",
                                    "protocol": "vless",
                                    "settings": {
                                        "clients": [
                                            {"id": "old-sui-uuid", "email": "sui-user"}
                                        ]
                                    },
                                }
                            ]
                        ),
                    }
                ),
                encoding="utf-8",
            )
            args = converter.build_parser().parse_args(
                ["revoke-source-clients", "--state-file", str(state_file)]
            )
            with mock.patch.object(converter.os, "geteuid", return_value=0), mock.patch.object(
                converter, "validate_xray_config"
            ), mock.patch.object(converter.os, "chown"), mock.patch.object(
                converter, "service_active", return_value=True
            ), mock.patch.object(converter, "service_action") as service_action, mock.patch.object(
                converter, "wait_for_ports", return_value=set()
            ), contextlib.redirect_stderr(io.StringIO()):
                exit_code = converter.command_revoke_source_clients(args)

            updated = json.loads(xray_config.read_text(encoding="utf-8"))
            updated_clients = updated["inbounds"][0]["settings"]["clients"]
            state = json.loads(state_file.read_text(encoding="utf-8"))
            backup_exists = Path(state["source_client_revoke_backup"]).exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [client["id"] for client in updated_clients],
            ["mmwx-admin-uuid", "package-uuid"],
        )
        self.assertEqual(state["status"], "source_clients_revoked")
        self.assertTrue(backup_exists)
        service_action.assert_called_once_with("xray", "restart")

    def test_revoke_source_clients_requires_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            xray_config = root / "xray.json"
            state_file = root / "state.json"
            source = {
                "tag": "vless-main",
                "protocol": "vless",
                "settings": {"clients": [{"id": "old-sui-uuid"}]},
            }
            xray_config.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                **source,
                                "port": 443,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state_file.write_text(
                json.dumps(
                    {
                        "xray_config": str(xray_config),
                        "source_credentials": converter.source_credential_records([source]),
                    }
                ),
                encoding="utf-8",
            )
            args = converter.build_parser().parse_args(
                ["revoke-source-clients", "--state-file", str(state_file)]
            )
            with mock.patch.object(converter.os, "geteuid", return_value=0):
                with self.assertRaisesRegex(converter.MigrationError, "no replacement"):
                    converter.command_revoke_source_clients(args)

            self.assertEqual(
                json.loads(xray_config.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"],
                [{"id": "old-sui-uuid"}],
            )

    def test_rollback_restarts_source_service_stopped_by_deploy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            xray_config = root / "xray.json"
            backup = root / "xray.backup.json"
            state_file = root / "state.json"
            xray_config.write_text(
                '{"inbounds": [{"tag": "ss-main", "port": 12315}]}\n', encoding="utf-8"
            )
            backup.write_text('{"inbounds": []}\n', encoding="utf-8")
            state_file.write_text(
                json.dumps(
                    {
                        "xray_config": str(xray_config),
                        "backup": str(backup),
                        "xray_service": "xray",
                        "agent_service": "mmw-agent",
                        "agent_config": "/etc/mmw-agent/config.yaml",
                        "deployed_tags": ["ss-main"],
                        "stopped_source_services": ["s-ui"],
                    }
                ),
                encoding="utf-8",
            )
            args = converter.build_parser().parse_args(
                ["rollback", "--state-file", str(state_file)]
            )
            with mock.patch.object(converter.os, "geteuid", return_value=0), mock.patch.object(
                converter.os, "chown"
            ), mock.patch.object(converter, "service_active", return_value=True), mock.patch.object(
                converter, "service_action"
            ) as service_action, contextlib.redirect_stderr(io.StringIO()):
                exit_code = converter.command_rollback(args)

            state = json.loads(state_file.read_text(encoding="utf-8"))
            restored = json.loads(xray_config.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(restored, {"inbounds": []})
        self.assertEqual(state["status"], "rolled_back")
        self.assertEqual(state["restarted_source_services"], ["s-ui"])
        service_action.assert_any_call("xray", "restart")
        service_action.assert_any_call("s-ui", "start")

    def test_converts_shadowsocks_and_socks(self):
        source = {
            "inbounds": [
                {
                    "type": "shadowsocks",
                    "tag": "ss-main",
                    "listen": "::",
                    "listen_port": 12315,
                    "method": "aes-256-gcm",
                    "password": "server-secret",
                },
                {
                    "type": "socks",
                    "tag": "socks-main",
                    "listen": "127.0.0.1",
                    "listen_port": 23620,
                    "users": [{"username": "alice", "password": "account-secret"}],
                },
            ]
        }

        result = converter.convert_config(source, self.options())

        self.assertEqual(len(result.inbounds), 2)
        shadowsocks, socks = result.inbounds
        self.assertEqual(shadowsocks["protocol"], "shadowsocks")
        self.assertEqual(shadowsocks["settings"]["method"], "aes-256-gcm")
        self.assertEqual(shadowsocks["settings"]["password"], "server-secret")
        self.assertEqual(shadowsocks["settings"]["network"], "tcp,udp")
        self.assertEqual(
            socks["settings"]["accounts"],
            [{"user": "alice", "pass": "account-secret"}],
        )
        self.assertTrue(socks["settings"]["udp"])

    def test_converts_vless_reality_and_derives_public_key(self):
        source = {
            "inbounds": [
                {
                    "type": "vless",
                    "tag": "vless-reality",
                    "listen": "::",
                    "listen_port": 443,
                    "users": [
                        {
                            "name": "alice",
                            "uuid": "11111111-2222-3333-4444-555555555555",
                            "flow": "xtls-rprx-vision",
                        }
                    ],
                    "tls": {
                        "enabled": True,
                        "server_name": "www.example.com",
                        "reality": {
                            "enabled": True,
                            "handshake": {"server": "www.example.com", "server_port": 443},
                            "private_key": "private-key",
                            "short_id": ["0123456789abcdef"],
                        },
                    },
                }
            ]
        }
        options = self.options(derive_reality_keys=True)

        with mock.patch.object(
            converter, "derive_reality_public_key", return_value="derived-public-key"
        ):
            result = converter.convert_config(source, options)

        inbound = result.inbounds[0]
        self.assertEqual(inbound["settings"]["clients"][0]["email"], "alice")
        self.assertEqual(inbound["settings"]["clients"][0]["flow"], "xtls-rprx-vision")
        reality = inbound["streamSettings"]["realitySettings"]
        self.assertEqual(inbound["streamSettings"]["security"], "reality")
        self.assertEqual(reality["dest"], "www.example.com:443")
        self.assertEqual(reality["serverNames"], ["www.example.com"])
        self.assertEqual(reality["publicKey"], "derived-public-key")
        self.assertEqual(reality["shortIds"], ["0123456789abcdef"])

    def test_reality_requires_public_key_in_strict_mode(self):
        source = {
            "inbounds": [
                {
                    "type": "vless",
                    "tag": "reality-no-public",
                    "listen_port": 443,
                    "users": [{"uuid": "11111111-2222-3333-4444-555555555555"}],
                    "tls": {
                        "enabled": True,
                        "reality": {
                            "handshake": {"server": "example.com", "server_port": 443},
                            "private_key": "private-key",
                            "short_id": ["abcd1234"],
                        },
                    },
                }
            ]
        }

        with self.assertRaisesRegex(converter.MigrationError, "public key is unavailable"):
            converter.convert_config(source, self.options())

    def test_derives_public_key_from_current_xray_output(self):
        completed = mock.Mock(
            returncode=0,
            stdout=(
                "PrivateKey: generated-private\n"
                "Password (PublicKey): generated-public\n"
                "Hash32: generated-hash\n"
            ),
        )
        with mock.patch.object(converter.subprocess, "run", return_value=completed):
            public_key = converter.derive_reality_public_key("private-key", "/bin/true")

        self.assertEqual(public_key, "generated-public")

    def test_converts_vmess_websocket_tls(self):
        source = {
            "inbounds": [
                {
                    "type": "vmess",
                    "tag": "vmess-wss",
                    "listen_port": 8443,
                    "users": [
                        {
                            "name": "bob",
                            "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            "alter_id": 0,
                        }
                    ],
                    "transport": {
                        "type": "ws",
                        "path": "/original",
                        "headers": {"Host": "node.example.com"},
                    },
                    "tls": {
                        "enabled": True,
                        "server_name": "node.example.com",
                        "alpn": ["http/1.1"],
                        "certificate_path": "/etc/ssl/fullchain.pem",
                        "key_path": "/etc/ssl/private.key",
                    },
                }
            ]
        }

        inbound = converter.convert_config(source, self.options()).inbounds[0]

        stream = inbound["streamSettings"]
        self.assertEqual(stream["network"], "ws")
        self.assertEqual(stream["security"], "tls")
        self.assertEqual(stream["wsSettings"]["path"], "/original")
        self.assertEqual(stream["wsSettings"]["headers"]["Host"], "node.example.com")
        self.assertEqual(stream["tlsSettings"]["serverName"], "node.example.com")
        self.assertEqual(
            stream["tlsSettings"]["certificates"][0]["certificateFile"],
            "/etc/ssl/fullchain.pem",
        )

    def test_converts_hysteria2_salamander(self):
        source = {
            "inbounds": [
                {
                    "type": "hysteria2",
                    "tag": "hy2",
                    "listen_port": 24443,
                    "users": [{"name": "carol", "password": "hy2-auth"}],
                    "obfs": {"type": "salamander", "password": "obfs-secret"},
                    "tls": {
                        "enabled": True,
                        "server_name": "hy.example.com",
                        "alpn": ["h3"],
                        "certificate_path": "/etc/ssl/hy.pem",
                        "key_path": "/etc/ssl/hy.key",
                    },
                }
            ]
        }

        inbound = converter.convert_config(source, self.options()).inbounds[0]

        self.assertEqual(inbound["protocol"], "hysteria")
        self.assertEqual(inbound["settings"]["version"], 2)
        self.assertEqual(inbound["settings"]["clients"][0]["auth"], "hy2-auth")
        self.assertEqual(inbound["streamSettings"]["network"], "hysteria")
        self.assertEqual(inbound["streamSettings"]["hysteriaSettings"]["version"], 2)
        self.assertEqual(
            inbound["streamSettings"]["hysteriaSettings"]["password"], "obfs-secret"
        )

    def test_skips_unsupported_or_fails_in_strict_mode(self):
        source = {
            "inbounds": [
                {"type": "tuic", "tag": "tuic", "listen_port": 10001},
                {
                    "type": "shadowsocks",
                    "tag": "ss",
                    "listen_port": 10002,
                    "method": "aes-128-gcm",
                    "password": "secret",
                },
            ]
        }

        result = converter.convert_config(
            source, self.options(strict=False, derive_reality_keys=False)
        )
        self.assertEqual([item["tag"] for item in result.inbounds], ["ss"])
        self.assertEqual(len(result.skipped), 1)

        with self.assertRaisesRegex(converter.MigrationError, "unsupported protocol"):
            converter.convert_config(source, self.options(strict=True))

    def test_port_mapping_and_tag_suffix(self):
        source = {
            "inbounds": [
                {
                    "type": "shadowsocks",
                    "tag": "ss",
                    "listen_port": 12000,
                    "method": "aes-128-gcm",
                    "password": "secret",
                }
            ]
        }
        options = self.options(port_offset=1000, port_map={12000: 32000}, tag_suffix="-stage")

        inbound = converter.convert_config(source, options).inbounds[0]

        self.assertEqual(inbound["port"], 32000)
        self.assertEqual(inbound["tag"], "ss-stage")

    def test_merge_preserves_control_config_and_replaces_only_matching_tag(self):
        original = {
            "api": {"tag": "api"},
            "stats": {},
            "policy": {"system": {"statsInboundUplink": True}},
            "metrics": {"tag": "metrics"},
            "routing": {"rules": [{"type": "field", "outboundTag": "api"}]},
            "outbounds": [{"tag": "direct", "protocol": "freedom"}],
            "inbounds": [
                {"tag": "api", "port": 10085, "protocol": "dokodemo-door"},
                {"tag": "replace-me", "port": 20000, "protocol": "socks"},
            ],
        }
        replacement = {
            "tag": "replace-me",
            "listen": "::",
            "port": 21000,
            "protocol": "shadowsocks",
            "settings": {"method": "aes-128-gcm", "password": "secret"},
        }

        merged = converter.merge_inbounds(original, [replacement], replace_existing=True)

        self.assertEqual(merged["api"], original["api"])
        self.assertEqual(merged["stats"], original["stats"])
        self.assertEqual(merged["routing"], original["routing"])
        self.assertEqual(merged["outbounds"], original["outbounds"])
        self.assertEqual(len(merged["inbounds"]), 2)
        self.assertEqual(merged["inbounds"][1], replacement)
        self.assertEqual(original["inbounds"][1]["port"], 20000)

    def test_merge_rejects_existing_tag_and_port_collision(self):
        original = {
            "inbounds": [
                {"tag": "api", "port": 10085, "protocol": "dokodemo-door"},
                {"tag": "existing", "port": 20000, "protocol": "socks"},
            ]
        }
        same_tag = {"tag": "existing", "port": 21000, "protocol": "socks"}
        same_port = {"tag": "new", "port": 20000, "protocol": "socks"}

        with self.assertRaisesRegex(converter.MigrationError, "already contains tag"):
            converter.merge_inbounds(copy.deepcopy(original), [same_tag], replace_existing=False)
        with self.assertRaisesRegex(converter.MigrationError, "conflicts with existing inbound"):
            converter.merge_inbounds(copy.deepcopy(original), [same_port], replace_existing=False)

    def test_load_agent_connection_supports_quoted_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "# generated\nmaster_url: \"https://master.example.com/\"\n"
                "token: 'server-token'\nconnection_mode: websocket\n",
                encoding="utf-8",
            )

            master_url, token = converter.load_agent_connection(config)

        self.assertEqual(master_url, "https://master.example.com")
        self.assertEqual(token, "server-token")

    def test_request_master_node_sync_verifies_persisted_tags(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "success": True,
                "server_id": 9,
                "server_name": "edge",
                "node_tags": ["ss-main", "socks-main"],
            }
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "master_url: https://master.example.com\ntoken: server-token\n",
                encoding="utf-8",
            )
            with mock.patch.object(converter.urllib.request, "urlopen", return_value=response) as urlopen:
                result = converter.request_master_node_sync(
                    config,
                    expected_tags=["ss-main", "socks-main"],
                    timeout=12,
                )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://master.example.com/api/remote/sync-nodes")
        self.assertEqual(request.get_header("User-agent"), converter.AGENT_USER_AGENT)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 12)
        self.assertEqual(result["server_id"], 9)

    def test_request_master_node_sync_rejects_missing_tag(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"success": True, "node_tags": ["ss-main"]}
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "master_url: https://master.example.com\ntoken: server-token\n",
                encoding="utf-8",
            )
            with mock.patch.object(converter.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(converter.MigrationError, "socks-main"):
                    converter.request_master_node_sync(
                        config, expected_tags=["ss-main", "socks-main"]
                    )

    def test_request_master_node_sync_marks_manual_sync_required(self):
        error = urllib.error.HTTPError(
            "https://master.example.com/api/remote/sync-nodes",
            404,
            "Not Found",
            {},
            io.BytesIO(b"not found"),
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "master_url: https://master.example.com\ntoken: server-token\n",
                encoding="utf-8",
            )
            with mock.patch.object(converter.urllib.request, "urlopen", side_effect=error):
                with self.assertRaisesRegex(converter.MasterSyncUnavailable, "Agent sync API"):
                    converter.request_master_node_sync(config, expected_tags=["ss-main"])


if __name__ == "__main__":
    unittest.main()
