output "push_role_arn" {
  value = aws_iam_role.push.arn
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}
