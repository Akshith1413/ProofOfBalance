#!/usr/bin/env python3
"""AWS checks for scripts/setup.sh and scripts/teardown.sh (boto3).

Credentials come from the environment (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
/ optional AWS_SESSION_TOKEN, loaded from .env). Nothing secret is printed.

Subcommands (exit 0 on success; non-zero with a plain-English reason on stderr):
  whoami                         account id and caller ARN
  my-ip                          every public IPv4 this machine uses, comma-separated
  ensure-bucket NAME             create the versioned, encrypted, private state bucket if missing
  bucket-exists NAME             exit 0 if it exists and we own it, 1 if not
  oidc-exists                    print true/false: GitHub OIDC provider already in the account
  pick-instance-type TYPE        TYPE, or on a Free-plan account a Free-Tier type with >= 4 GiB
  find-tagged                    list resources tagged Project=ProofOfBalance (and what they cost)
  force-cleanup                  terminate/release cost-incurring tagged resources (instances, EIPs, volumes)
  purge-bucket NAME              delete every object version and the bucket itself
"""
from __future__ import annotations

import ipaddress
import os
import sys
import time
import urllib.request

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    print("boto3 is not installed: run scripts/setup.sh (it creates .venv) or pip install boto3", file=sys.stderr)
    sys.exit(4)

PROJECT_TAG = {"Key": "Project", "Values": ["ProofOfBalance"]}
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-south-1"
CFG = Config(region_name=REGION, retries={"max_attempts": 8, "mode": "adaptive"}, connect_timeout=10, read_timeout=30)

CREDENTIAL_ERRORS = {
    "InvalidClientTokenId": "The AWS access key id is not valid (deleted, mistyped, or for another partition).",
    "SignatureDoesNotMatch": "The AWS secret access key does not match the access key id (check for a typo or stray quote).",
    "ExpiredToken": "The AWS session token has expired; get fresh credentials.",
    "AccessDenied": "The credentials are valid but not allowed to do this.",
    "UnrecognizedClientException": "The AWS access key id is not recognised.",
}


def die(msg: str, code: int = 1) -> None:
    print(f"aws_helper: {msg}", file=sys.stderr)
    sys.exit(code)


def explain(exc: Exception) -> str:
    if isinstance(exc, NoCredentialsError):
        return "No AWS credentials found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env."
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return f"{code}: {CREDENTIAL_ERRORS.get(code, exc.response.get('Error', {}).get('Message', str(exc)))}"
    return str(exc)


def client(name: str):
    return boto3.client(name, config=CFG)


def whoami() -> None:
    try:
        ident = client("sts").get_caller_identity()
    except (ClientError, BotoCoreError) as exc:
        die(explain(exc), 3)
    arn = ident["Arn"]
    print(f"{ident['Account']} {arn}")
    if arn.endswith(":root"):
        print("aws_helper: warning: these are ROOT account keys. Create an IAM user/role instead and delete root keys.",
              file=sys.stderr)


IP_ENDPOINTS = ("https://checkip.amazonaws.com", "https://api.ipify.org", "https://ifconfig.me/ip")


def _fetch_ip(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            ip = r.read().decode().strip()
        return ip if isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address) else None
    except (OSError, ValueError):
        return None


def my_ip(samples: int = 24) -> None:
    """Print every public IPv4 this machine egresses from, comma-separated, most frequent first.

    Some networks (campuses, multi-WAN offices, mobile carriers) load-balance
    outgoing connections across several public IPs; whitelisting only the first
    one we see makes SSH and kubectl fail at random later.
    """
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    urls = [IP_ENDPOINTS[i % len(IP_ENDPOINTS)] for i in range(samples)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        seen = Counter(ip for ip in pool.map(_fetch_ip, urls) if ip)
    if not seen:
        die("could not detect this machine's public IPv4; set MY_IP in .env")
    ips = [ip for ip, _ in seen.most_common()]
    if len(ips) > 1:
        print(f"aws_helper: this network uses {len(ips)} public IPs ({', '.join(ips)}); allowing each as a /32",
              file=sys.stderr)
    print(",".join(ips))


def bucket_exists(name: str) -> bool:
    s3 = client("s3")
    try:
        s3.head_bucket(Bucket=name)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchBucket", "NotFound"):
            return False
        if code in ("403", "Forbidden"):
            die(f"bucket {name} exists but belongs to another AWS account (or you lack s3:ListBucket). "
                "Choose another TF_STATE_BUCKET in .env.", 5)
        die(explain(exc))
    return False


def ensure_bucket(name: str) -> None:
    s3 = client("s3")
    if bucket_exists(name):
        print(f"exists {name}")
    else:
        try:
            kwargs = {"Bucket": name}
            if REGION != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": REGION}
            s3.create_bucket(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "BucketAlreadyOwnedByYou":
                pass
            elif code == "BucketAlreadyExists":
                die(f"bucket name {name} is taken globally by someone else; set TF_STATE_BUCKET in .env", 5)
            elif code == "OperationAborted":
                time.sleep(10)
                return ensure_bucket(name)
            else:
                die(f"cannot create state bucket {name}: {explain(exc)}")
        s3.get_waiter("bucket_exists").wait(Bucket=name, WaiterConfig={"Delay": 2, "MaxAttempts": 30})
        print(f"created {name}")
    # Idempotent hardening, applied on every run.
    try:
        s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
        s3.put_bucket_encryption(Bucket=name, ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}, "BucketKeyEnabled": True}]})
        s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": [
            {"Key": "Project", "Value": "ProofOfBalance"}, {"Key": "Purpose", "Value": "terraform-state"}]})
    except ClientError as exc:
        die(f"bucket {name} exists but could not be hardened: {explain(exc)}")


def oidc_exists() -> None:
    try:
        providers = client("iam").list_open_id_connect_providers()["OpenIDConnectProviderList"]
    except ClientError as exc:
        die(explain(exc))
    print("true" if any(p["Arn"].endswith("/token.actions.githubusercontent.com") for p in providers) else "false")


MIN_MEMORY_MIB = 4 * 1024


def account_plan() -> dict:
    """FREE or PAID account plan (AWS Free Tier API). {} if it cannot be read."""
    try:
        resp = boto3.client("freetier", config=Config(region_name="us-east-1", retries={"max_attempts": 3}))\
            .get_account_plan_state()
        return {"type": resp.get("accountPlanType", ""), "credits": resp.get("accountPlanRemainingCredits", {}),
                "expires": str(resp.get("accountPlanExpirationDate", ""))}
    except (ClientError, BotoCoreError, AttributeError):
        return {}


def pick_instance_type(preferred: str) -> None:
    """Print an instance type this account can launch, preferring `preferred`.

    Accounts on the AWS "Free plan" may only launch Free-Tier-eligible types
    (EC2's DryRun does not check this; the real launch fails). On that plan,
    if `preferred` is not eligible, use the largest eligible x86_64 type with
    >= 4 GiB (m7i-flex.large today).
    """
    plan = account_plan()
    if plan.get("type") != "FREE":
        print(preferred)
        return
    credits = plan.get("credits") or {}
    print(f"aws_helper: account is on the AWS Free plan ({credits.get('amount', '?')} {credits.get('unit', 'USD')} "
          f"credits left, plan ends {plan.get('expires', '?')[:10]})", file=sys.stderr)
    ec2 = client("ec2")
    pages = ec2.get_paginator("describe_instance_types").paginate(
        Filters=[{"Name": "free-tier-eligible", "Values": ["true"]},
                 {"Name": "processor-info.supported-architecture", "Values": ["x86_64"]}])
    eligible = [t for p in pages for t in p["InstanceTypes"]]
    if any(t["InstanceType"] == preferred for t in eligible):
        print(preferred)
        return
    big_enough = sorted((t for t in eligible if t["MemoryInfo"]["SizeInMiB"] >= MIN_MEMORY_MIB),
                        key=lambda t: -t["MemoryInfo"]["SizeInMiB"])
    if not big_enough:
        die(f"{preferred} is not Free-Tier eligible and no eligible type has >= 4 GiB. "
            "Upgrade the account plan in the AWS Billing console, or set INSTANCE_TYPE.", 6)
    t = big_enough[0]
    print(f"aws_helper: {preferred} is not Free-Tier eligible; using {t['InstanceType']} "
          f"({t['VCpuInfo']['DefaultVCpus']} vCPU, {t['MemoryInfo']['SizeInMiB'] // 1024} GiB)", file=sys.stderr)
    print(t["InstanceType"])


def _tag_filter():
    return [{"Name": "tag:Project", "Values": PROJECT_TAG["Values"]}]


def find_tagged(quiet: bool = False) -> dict:
    ec2 = client("ec2")
    found = {"instances": [], "eips": [], "volumes": [], "vpcs": [], "key_pairs": [], "security_groups": []}
    try:
        for r in ec2.describe_instances(Filters=_tag_filter() + [
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]:
            for i in r["Instances"]:
                found["instances"].append(f"{i['InstanceId']} ({i['State']['Name']}, {i['InstanceType']})")
        for a in ec2.describe_addresses(Filters=_tag_filter())["Addresses"]:
            found["eips"].append(f"{a['AllocationId']} {a.get('PublicIp', '')}")
        for v in ec2.describe_volumes(Filters=_tag_filter())["Volumes"]:
            found["volumes"].append(f"{v['VolumeId']} ({v['State']}, {v['Size']} GB)")
        for v in ec2.describe_vpcs(Filters=_tag_filter())["Vpcs"]:
            found["vpcs"].append(v["VpcId"])
        for k in ec2.describe_key_pairs(Filters=_tag_filter())["KeyPairs"]:
            found["key_pairs"].append(k["KeyName"])
        for g in ec2.describe_security_groups(Filters=_tag_filter())["SecurityGroups"]:
            found["security_groups"].append(g["GroupId"])
    except (ClientError, BotoCoreError) as exc:
        die(f"could not list resources: {explain(exc)}")
    if not quiet:
        costly = found["instances"] + found["eips"] + found["volumes"]
        for kind, items in found.items():
            for item in items:
                print(f"{kind}: {item}")
        print(f"summary: {len(costly)} cost-incurring, {sum(map(len, found.values()))} total in {REGION}")
    return found


def force_cleanup() -> None:
    """Last resort when terraform state is lost: stop everything that bills by the hour."""
    ec2 = client("ec2")
    ids = []
    for r in ec2.describe_instances(Filters=_tag_filter() + [
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]:
        ids += [i["InstanceId"] for i in r["Instances"]]
    if ids:
        print(f"terminating instances: {' '.join(ids)}")
        ec2.terminate_instances(InstanceIds=ids)
        ec2.get_waiter("instance_terminated").wait(InstanceIds=ids, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    for a in ec2.describe_addresses(Filters=_tag_filter())["Addresses"]:
        if a.get("AssociationId"):
            ec2.disassociate_address(AssociationId=a["AssociationId"])
        print(f"releasing elastic ip {a.get('PublicIp')}")
        ec2.release_address(AllocationId=a["AllocationId"])
    for v in ec2.describe_volumes(Filters=_tag_filter() + [{"Name": "status", "Values": ["available"]}])["Volumes"]:
        print(f"deleting volume {v['VolumeId']}")
        ec2.delete_volume(VolumeId=v["VolumeId"])
    print("cost-incurring resources removed. Free leftovers (VPC, security group, IAM roles, budget) "
          "can be removed with terraform once state is available, or by hand in the console.")


def purge_bucket(name: str) -> None:
    if not bucket_exists(name):
        print(f"bucket {name} does not exist")
        return
    bucket = boto3.resource("s3", config=CFG).Bucket(name)
    bucket.object_versions.delete()
    bucket.delete()
    print(f"deleted bucket {name}")


def main(argv: list[str]) -> None:
    if not argv:
        die(__doc__, 64)
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "whoami":
            whoami()
        elif cmd == "my-ip":
            my_ip()
        elif cmd == "ensure-bucket" and len(args) == 1:
            ensure_bucket(args[0])
        elif cmd == "bucket-exists" and len(args) == 1:
            sys.exit(0 if bucket_exists(args[0]) else 1)
        elif cmd == "oidc-exists":
            oidc_exists()
        elif cmd == "pick-instance-type" and len(args) == 1:
            pick_instance_type(args[0])
        elif cmd == "find-tagged":
            find_tagged()
        elif cmd == "force-cleanup":
            force_cleanup()
        elif cmd == "purge-bucket" and len(args) == 1:
            purge_bucket(args[0])
        else:
            die(f"unknown command or wrong arguments: {' '.join(argv)}\n{__doc__}", 64)
    except (ClientError, BotoCoreError) as exc:
        die(explain(exc))


if __name__ == "__main__":
    main(sys.argv[1:])
