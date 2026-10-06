# GitHub Actions authenticates with short-lived OIDC tokens: no AWS access keys are stored in GitHub.
# Two roles, each trusted only for the exact claims the workflow presents:
#   - ecr-push: pushes images, only from the main branch
#   - deploy:   helm upgrade into one namespace, only from jobs that target the matching GitHub Environment
#               (add required reviewers to that Environment for the manual approval gate)

data "aws_partition" "current" {}

locals {
  provider_arn = var.create_provider ? aws_iam_openid_connect_provider.github[0].arn : var.existing_provider_arn
  sub_claim    = "token.actions.githubusercontent.com:sub"
  aud_claim    = "token.actions.githubusercontent.com:aud"
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # AWS validates GitHub's certificate chain itself; the thumbprint is required by the API but not used.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

# ------------------------------------------------------------ image push role
data "aws_iam_policy_document" "push_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = local.aud_claim
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = local.sub_claim
      values   = ["repo:${var.repository}:ref:refs/heads/main"]
    }
  }
}

data "aws_iam_policy_document" "push" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushToServiceRepositories"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeImageScanFindings",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = var.ecr_repository_arns
  }
}

resource "aws_iam_role" "push" {
  name                 = "${var.name}-github-ecr-push"
  assume_role_policy   = data.aws_iam_policy_document.push_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "push" {
  name   = "push-images"
  role   = aws_iam_role.push.id
  policy = data.aws_iam_policy_document.push.json
}

# ------------------------------------------------------------ deploy role
data "aws_iam_policy_document" "deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = local.aud_claim
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = local.sub_claim
      values   = ["repo:${var.repository}:environment:${var.environment}"]
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "DescribeCluster"
    actions   = ["eks:DescribeCluster"]
    resources = [var.cluster_arn]
  }

  # Environment wiring (registry, queue URL, IRSA role ARNs) published by Terraform.
  statement {
    sid       = "ReadHelmValues"
    actions   = ["ssm:GetParameter"]
    resources = [var.helm_values_parameter_arn]
  }

  # Lets the deploy job read the image scan result before promoting (optional smoke check).
  statement {
    sid       = "ReadScanFindings"
    actions   = ["ecr:DescribeImages", "ecr:DescribeImageScanFindings"]
    resources = var.ecr_repository_arns
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.name}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.deploy_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-to-eks"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# Kubernetes-side permission: edit rights in the application namespace only (not cluster-wide).
# If `helm upgrade` is denied on ServiceMonitor/PrometheusRule objects, switch this association to
# AmazonEKSAdminPolicy (still namespace-scoped), because the Edit policy does not cover every custom resource.
resource "aws_eks_access_entry" "deploy" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.deploy.arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "deploy" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.deploy.arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"

  access_scope {
    type       = "namespace"
    namespaces = [var.namespace]
  }

  depends_on = [aws_eks_access_entry.deploy]
}
