# GitHub Actions -> AWS with OIDC: every workflow run gets short-lived
# credentials. No access keys stored in GitHub, nothing to leak or rotate.

locals {
  github_oidc_host = "token.actions.githubusercontent.com"
  github_oidc_arn = (var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
  : data.aws_iam_openid_connect_provider.github[0].arn)
  account_id     = data.aws_caller_identity.current.account_id
  partition      = data.aws_partition.current.partition
  role_arn_glob  = "arn:${local.partition}:iam::${local.account_id}:role/${var.name}-*"
  budget_arn     = "arn:${local.partition}:budgets::${local.account_id}:budget/${var.name}-*"
  has_state      = var.state_bucket != ""
  state_bucket   = "arn:${local.partition}:s3:::${var.state_bucket}"
  state_objects  = "arn:${local.partition}:s3:::${var.state_bucket}/proofofbalance/*"
  oidc_arn_match = "arn:${local.partition}:iam::${local.account_id}:oidc-provider/${local.github_oidc_host}"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://${local.github_oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://${local.github_oidc_host}"
}

# ---------------------------------------------------------------- plan role --
# Trusted by any workflow in this repository (PRs included). Read-only for the
# resources this stack manages, plus the state bucket (plan writes a lock file).
data "aws_iam_policy_document" "plan_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "${local.github_oidc_host}:sub"
      values   = ["repo:${var.github_repo}:*"]
    }
  }
}

data "aws_iam_policy_document" "plan" {
  statement {
    sid       = "ReadEc2"
    actions   = ["ec2:Describe*"]
    resources = ["*"]
  }
  statement {
    sid = "ReadOwnIam"
    actions = [
      "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = [local.role_arn_glob]
  }
  statement {
    sid       = "ReadOidcProvider"
    actions   = ["iam:GetOpenIDConnectProvider", "iam:ListOpenIDConnectProviders"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadBudget"
    actions   = ["budgets:ViewBudget", "budgets:ListTagsForResource"]
    resources = [local.budget_arn]
  }
  statement {
    sid       = "Identity"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }
  dynamic "statement" {
    for_each = local.has_state ? [1] : []
    content {
      sid       = "StateBucketList"
      actions   = ["s3:ListBucket"]
      resources = [local.state_bucket]
    }
  }
  dynamic "statement" {
    for_each = local.has_state ? [1] : []
    content {
      sid       = "StateObjects"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      resources = [local.state_objects]
    }
  }
}

resource "aws_iam_role" "github_plan" {
  name                 = "${var.name}-github-plan"
  description          = "GitHub Actions terraform plan for ${var.github_repo} (read-only)"
  assume_role_policy   = data.aws_iam_policy_document.plan_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "github_plan" {
  name   = "terraform-plan"
  role   = aws_iam_role.github_plan.id
  policy = data.aws_iam_policy_document.plan.json
}

# --------------------------------------------------------------- apply role --
# Only the protected "production" environment (manual approval) can assume it.
data "aws_iam_policy_document" "apply_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:sub"
      values   = ["repo:${var.github_repo}:environment:production"]
    }
  }
}

data "aws_iam_policy_document" "apply" {
  source_policy_documents = [data.aws_iam_policy_document.plan.json]

  statement {
    sid       = "Ec2InThisRegion"
    actions   = ["ec2:*"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }
  }
  statement {
    sid = "ManageOwnRoles"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
      "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole",
    ]
    resources = [local.role_arn_glob]
  }
  statement {
    sid = "ManageGithubOidcProvider"
    actions = [
      "iam:CreateOpenIDConnectProvider", "iam:DeleteOpenIDConnectProvider", "iam:TagOpenIDConnectProvider",
      "iam:UntagOpenIDConnectProvider", "iam:UpdateOpenIDConnectProviderThumbprint",
      "iam:AddClientIDToOpenIDConnectProvider", "iam:RemoveClientIDFromOpenIDConnectProvider",
    ]
    resources = [local.oidc_arn_match]
  }
  statement {
    sid       = "ManageBudget"
    actions   = ["budgets:ModifyBudget", "budgets:TagResource", "budgets:UntagResource"]
    resources = [local.budget_arn]
  }
}

resource "aws_iam_role" "github_apply" {
  name                 = "${var.name}-github-apply"
  description          = "GitHub Actions terraform apply for ${var.github_repo} (environment: production)"
  assume_role_policy   = data.aws_iam_policy_document.apply_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "github_apply" {
  name   = "terraform-apply"
  role   = aws_iam_role.github_apply.id
  policy = data.aws_iam_policy_document.apply.json
}
