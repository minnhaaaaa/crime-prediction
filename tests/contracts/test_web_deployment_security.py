from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_vercel_allows_only_same_origin_camera_capture() -> None:
    config = json.loads((ROOT / "src" / "web" / "vercel.json").read_text())
    headers = {
        item["key"]: item["value"]
        for rule in config["headers"]
        if rule["source"] == "/(.*)"
        for item in rule["headers"]
    }
    assert headers["Permissions-Policy"] == (
        "camera=(self), microphone=(), geolocation=()"
    )
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_aws_host_requires_imdsv2_and_has_no_unrestricted_egress() -> None:
    host_template = (ROOT / "deploy" / "aws-vm" / "review2-host.yml").read_text()
    foundation_template = (
        ROOT / "deploy" / "aws-vm" / "review2-foundation.yml"
    ).read_text()

    # Keep these deployment invariants explicit without trying to interpret
    # CloudFormation's intrinsic YAML tags with a generic parser.
    assert "HttpTokens: required" in host_template
    assert "HttpPutResponseHopLimit: 2" in host_template
    assert "MetadataOptions:" in host_template

    app_group = foundation_template.split("  AppSecurityGroup:", 1)[1].split(
        "  AppHttpsEgress:", 1
    )[0]
    assert "SecurityGroupEgress: []" in app_group
    assert "IpProtocol: -1" not in foundation_template
    assert "IpProtocol: udp" not in foundation_template
    assert "AppDns" not in foundation_template
    assert "AppNtp" not in foundation_template
    assert "DestinationSecurityGroupId: !Ref DatabaseSecurityGroup" in foundation_template
    assert (
        "DestinationSecurityGroupId: !Ref ModelRegistrySecurityGroup"
        in foundation_template
    )

    assert "https://github\\.com/" in host_template
    assert "[A-Za-z0-9][A-Za-z0-9._/-]{0,127}" in host_template
    assert host_template.count("AllowedPattern: 'v[0-9]+\\.[0-9]+\\.[0-9]+'") == 2
    assert "AllowedPattern: 'fs-[0-9a-f]+'" in host_template
    assert "AllowedPattern: 'fsap-[0-9a-f]+'" in host_template


def test_aws_proxy_allows_same_origin_camera_and_bounds_uploads() -> None:
    nginx = (ROOT / "docker" / "nginx.conf").read_text(encoding="utf-8")
    compose = (ROOT / "deploy" / "aws-vm" / "compose.yml").read_text(
        encoding="utf-8"
    )
    environment = (
        ROOT / "deploy" / "aws-vm" / ".env.production.example"
    ).read_text(encoding="utf-8")

    assert 'Permissions-Policy "camera=(self), microphone=(), geolocation=()"' in nginx
    assert "client_max_body_size 9m;" in nginx
    assert "proxy_set_header Host localhost;" in nginx
    assert "resolver 127.0.0.11 ipv6=off valid=10s;" in nginx
    assert "server api:8000 resolve;" in nginx
    assert "proxy_pass http://api_backend;" in nginx
    assert "MAX_REQUEST_BYTES: ${MAX_REQUEST_BYTES:-9437184}" in compose
    assert (
        "TRUSTED_HOSTS: localhost,127.0.0.1,${TRUSTED_HOSTS:?Set TRUSTED_HOSTS}"
        in compose
    )
    assert "PUBLIC_HLS_DEMO_ENABLED: ${PUBLIC_HLS_DEMO_ENABLED:-true}" in compose
    assert "PUBLIC_HLS_DEMO_ENABLED=true" in environment
    assert "WEB_BIND_ADDRESS=0.0.0.0" in environment


def test_production_composition_reports_its_real_deployment_mode() -> None:
    composition = (
        ROOT / "src" / "data" / "video" / "production_app.py"
    ).read_text(encoding="utf-8")
    assert 'deployment_mode="production"' in composition


def test_frontend_security_config_is_valid_json() -> None:
    # This intentionally remains a tiny parser smoke test so malformed headers
    # fail locally before Vercel sees them.
    config = json.loads((ROOT / "src" / "web" / "vercel.json").read_text())
    assert isinstance(config["headers"], list)
