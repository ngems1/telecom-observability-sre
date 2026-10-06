# Prod: redundancy on, longer retention, deletion protection. Apply with:  terraform apply -var-file=envs/prod.tfvars
# Use a separate state key (see backend-prod example) and, ideally, a separate AWS account.
environment = "prod"
region      = "us-east-1"

az_count           = 3
single_nat_gateway = false

node_instance_types = ["t3.large"]
node_capacity_type  = "ON_DEMAND"
node_min_size       = 3
node_desired_size   = 3
node_max_size       = 6
api_allowed_cidrs   = ["0.0.0.0/0"] # restrict before real use

db_instance_class        = "db.t4g.small"
db_multi_az              = true
db_backup_retention_days = 14
db_deletion_protection   = true
db_skip_final_snapshot   = false
db_performance_insights  = true
secret_recovery_days     = 7

log_retention_days        = 30
cloudtrail_retention_days = 365
alert_email               = ""

github_repository = "ngems1/telecom-observability-sre"

# If prod shares the AWS account with dev, the account-level and shared resources already exist:
#   - ECR repositories are shared (images are promoted by SHA, not rebuilt)
#   - one GitHub OIDC provider per account (also set github_oidc_provider_arn)
#   - CloudTrail / GuardDuty / Inspector / Security Hub are per account (and region)
create_ecr_repositories     = false
create_github_oidc_provider = true # set false (and github_oidc_provider_arn) when dev already created it
security_services = {
  cloudtrail  = false
  guardduty   = false
  inspector   = false
  securityhub = false
}
