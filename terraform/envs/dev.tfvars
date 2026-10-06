# Dev: cheapest sensible footprint, easy to tear down. Apply with:  terraform apply -var-file=envs/dev.tfvars
environment = "dev"
region      = "us-east-1"

# Network
az_count           = 2
single_nat_gateway = true # one NAT gateway (~$33/month each); prod uses one per AZ

# EKS
node_instance_types = ["t3.large"] # kube-prometheus-stack + the app need ~6 GiB; t3.medium is too tight
node_capacity_type  = "ON_DEMAND"  # "SPOT" saves ~60-70% but nodes can disappear mid-demo
node_min_size       = 2
node_desired_size   = 2
node_max_size       = 4
# Restrict the Kubernetes API to your own IP where you can, e.g. ["203.0.113.7/32"]
api_allowed_cidrs = ["0.0.0.0/0"]

# RDS
db_instance_class        = "db.t4g.micro"
db_multi_az              = false
db_backup_retention_days = 3
db_deletion_protection   = false
db_skip_final_snapshot   = true
secret_recovery_days     = 0

# Observability / security
log_retention_days        = 14
cloudtrail_retention_days = 90
alert_email               = "" # put your email to receive alarm notifications (confirm the subscription)

# CI/CD
github_repository = "ngems1/telecom-observability-sre"
