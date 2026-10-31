"""scripts/aws_helper.py with stubbed AWS (no network, no credentials)."""
import io

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.stub import Stubber

from conftest import ROOT, load_module

helper = load_module("pob_aws_helper", ROOT / "scripts" / "aws_helper.py")


def err(code, op="Op"):
    return ClientError({"Error": {"Code": code, "Message": f"{code} message"}}, op)


@pytest.mark.parametrize("code,needle", [
    ("InvalidClientTokenId", "access key id is not valid"),
    ("SignatureDoesNotMatch", "secret access key does not match"),
    ("ExpiredToken", "expired"),
    ("SomethingElse", "SomethingElse message"),
])
def test_explain_credential_errors(code, needle):
    assert needle in helper.explain(err(code))


def test_explain_no_credentials():
    assert "AWS_ACCESS_KEY_ID" in helper.explain(NoCredentialsError())


@pytest.fixture
def s3(monkeypatch):
    import boto3
    client = boto3.client("s3", region_name="ap-south-1", aws_access_key_id="AKIAXXXXXXXXXXXXXXXX",
                          aws_secret_access_key="x" * 40)
    monkeypatch.setattr(helper, "client", lambda name: client)
    with Stubber(client) as stub:
        yield stub


def test_bucket_exists_true_false(s3):
    s3.add_response("head_bucket", {}, {"Bucket": "b"})
    assert helper.bucket_exists("b") is True
    s3.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
    assert helper.bucket_exists("b") is False


def test_bucket_owned_by_someone_else_exits_5(s3):
    s3.add_client_error("head_bucket", service_error_code="403", http_status_code=403)
    with pytest.raises(SystemExit) as exc:
        helper.bucket_exists("b")
    assert exc.value.code == 5


def test_ensure_bucket_existing_is_hardened_idempotently(s3, capsys):
    s3.add_response("head_bucket", {}, {"Bucket": "b"})
    for op in ("put_bucket_versioning", "put_bucket_encryption", "put_public_access_block", "put_bucket_tagging"):
        s3.add_response(op, {})
    helper.ensure_bucket("b")
    assert "exists b" in capsys.readouterr().out
    s3.assert_no_pending_responses()


def test_ensure_bucket_name_taken_globally(s3):
    s3.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
    s3.add_client_error("create_bucket", service_error_code="BucketAlreadyExists", http_status_code=409)
    with pytest.raises(SystemExit) as exc:
        helper.ensure_bucket("b")
    assert exc.value.code == 5


def test_my_ip_collects_every_egress_ip(monkeypatch, capsys):
    answers = iter([None, "203.0.113.7", "198.51.100.2", "203.0.113.7"] * 10)
    monkeypatch.setattr(helper, "_fetch_ip", lambda url: next(answers))
    helper.my_ip(samples=8)
    out = capsys.readouterr()
    assert out.out.strip() == "203.0.113.7,198.51.100.2" and "2 public IPs" in out.err


def test_my_ip_validates_answers(monkeypatch):
    import io
    replies = {"a": b"not-an-ip", "b": b"2001:db8::1", "c": b"203.0.113.9\n"}
    monkeypatch.setattr(helper.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(replies[url]))
    assert helper._fetch_ip("a") is None and helper._fetch_ip("b") is None and helper._fetch_ip("c") == "203.0.113.9"


def test_my_ip_none_found_exits(monkeypatch):
    monkeypatch.setattr(helper, "_fetch_ip", lambda url: None)
    with pytest.raises(SystemExit):
        helper.my_ip(samples=3)


def test_unknown_command_exits_64():
    with pytest.raises(SystemExit) as exc:
        helper.main(["bogus"])
    assert exc.value.code == 64


def _types(*specs):
    return {"InstanceTypes": [{"InstanceType": n, "MemoryInfo": {"SizeInMiB": m}, "VCpuInfo": {"DefaultVCpus": 2}}
                              for n, m in specs]}


@pytest.fixture
def ec2(monkeypatch):
    import boto3
    client = boto3.client("ec2", region_name="ap-south-1", aws_access_key_id="AKIAXXXXXXXXXXXXXXXX",
                          aws_secret_access_key="x" * 40)
    monkeypatch.setattr(helper, "client", lambda name: client)
    with Stubber(client) as stub:
        yield stub


def test_paid_account_keeps_preferred_type(monkeypatch, capsys):
    monkeypatch.setattr(helper, "account_plan", lambda: {"type": "PAID"})
    helper.pick_instance_type("t3.medium")
    assert capsys.readouterr().out.strip() == "t3.medium"


def test_free_plan_swaps_to_largest_eligible(monkeypatch, ec2, capsys):
    monkeypatch.setattr(helper, "account_plan", lambda: {"type": "FREE", "credits": {"amount": 120}, "expires": "2027"})
    ec2.add_response("describe_instance_types", _types(("t3.small", 2048), ("c7i-flex.large", 4096), ("m7i-flex.large", 8192)))
    helper.pick_instance_type("t3.medium")
    out = capsys.readouterr()
    assert out.out.strip() == "m7i-flex.large" and "not Free-Tier eligible" in out.err


def test_free_plan_keeps_eligible_preferred(monkeypatch, ec2, capsys):
    monkeypatch.setattr(helper, "account_plan", lambda: {"type": "FREE"})
    ec2.add_response("describe_instance_types", _types(("c7i-flex.large", 4096), ("m7i-flex.large", 8192)))
    helper.pick_instance_type("c7i-flex.large")
    assert capsys.readouterr().out.strip() == "c7i-flex.large"


def test_free_plan_without_big_enough_type_exits_6(monkeypatch, ec2):
    monkeypatch.setattr(helper, "account_plan", lambda: {"type": "FREE"})
    ec2.add_response("describe_instance_types", _types(("t3.micro", 1024)))
    with pytest.raises(SystemExit) as exc:
        helper.pick_instance_type("t3.medium")
    assert exc.value.code == 6


def test_unknown_plan_keeps_preferred(monkeypatch, capsys):
    monkeypatch.setattr(helper, "account_plan", lambda: {})
    helper.pick_instance_type("t3.medium")
    assert capsys.readouterr().out.strip() == "t3.medium"
