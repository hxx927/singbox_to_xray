import copy
import io
import json
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

    def test_request_master_node_sync_explains_old_master(self):
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
                with self.assertRaisesRegex(converter.MigrationError, "upgrade miaomiaowuX"):
                    converter.request_master_node_sync(config, expected_tags=["ss-main"])


if __name__ == "__main__":
    unittest.main()
