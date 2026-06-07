"""Tests for app.host_identity — location awareness block."""
from app import host_identity


def _fresh():
    host_identity.detect.cache_clear()


def test_local_fallback_when_no_imds(monkeypatch):
    """Off EC2 (no IMDS token), detect() reports a local machine."""
    _fresh()
    monkeypatch.setattr(host_identity, "_imds_token", lambda: None)
    h = host_identity.detect()
    assert h["environment"] == "local"
    assert h["instance_id"] is None
    assert h["hostname"]
    _fresh()


def test_local_block_text(monkeypatch):
    _fresh()
    monkeypatch.setattr(host_identity, "_imds_token", lambda: None)
    block = host_identity.host_identity_block()
    assert "WHERE YOU ARE RUNNING" in block
    assert "local / non-EC2" in block
    _fresh()


def test_ec2_detect_and_block(monkeypatch):
    """On EC2, detect() pulls metadata and the block names the instance and
    the self-exec path."""
    _fresh()
    monkeypatch.setattr(host_identity, "_imds_token", lambda: "tok")
    fake = {
        "instance-id": "i-02c699d3d7efbdc82",
        "instance-type": "t3.medium",
        "placement/availability-zone": "us-east-1d",
        "placement/region": "us-east-1",
        "public-ipv4": "100.52.234.163",
        "local-ipv4": "10.0.0.158",
        "ami-id": "ami-00403f401ee6a4b98",
    }
    monkeypatch.setattr(host_identity, "_imds_get", lambda path, token: fake.get(path))
    h = host_identity.detect()
    assert h["environment"] == "ec2"
    assert h["instance_id"] == "i-02c699d3d7efbdc82"
    assert h["instance_type"] == "t3.medium"
    block = host_identity.host_identity_block()
    assert "i-02c699d3d7efbdc82" in block
    assert "t3.medium" in block
    assert "ssh_run(host='autopilot'" in block
    assert "passwordless sudo" in block
    _fresh()


def test_system_prompt_includes_host_block(monkeypatch):
    """build_system_prompt() embeds the live host block."""
    from app import context
    _fresh()
    monkeypatch.setattr(host_identity, "_imds_token", lambda: None)
    prompt = context.build_system_prompt()
    assert "WHERE YOU ARE RUNNING" in prompt
    assert "TrueSight DAO Autopilot" in prompt
    _fresh()
