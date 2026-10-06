# Convenience targets. Requires Docker (compose v2) and, for tests, Python 3.12+.

.PHONY: up up-observability down logs loadgen test test-unit

up:                  ## Build and start postgres, localstack (SQS), usage-api, notification-service
	docker compose up --build -d

up-observability:    ## Same, plus Prometheus (:9090) and Grafana (:3000)
	docker compose --profile observability up --build -d

loadgen:             ## Start the traffic generator (5 rps)
	docker compose --profile loadgen up --build -d loadgen

down:                ## Stop everything and remove volumes
	docker compose --profile loadgen --profile observability down -v

logs:
	docker compose logs -f usage-api notification-service

test-unit:           ## Fast unit tests (need: pip install prometheus-client starlette)
	cd services/usage-api && python -m unittest discover -s tests -p "test_logic.py"
	cd services/usage-api && python -m unittest discover -s tests -p "test_chaos.py"
	cd services/notification-service && python -m unittest discover -s tests -p "test_processor.py"

test:                ## Full suites (needs: pip install -r requirements-dev.txt in each service)
	cd services/usage-api && python -m pytest
	cd services/notification-service && python -m pytest

# ---------------------------------------------------------------- same checks as CI (.github/workflows/ci-cd.yml)
.PHONY: lint helm-check tf-check ci-local

lint:                ## ruff on both services (pip install ruff)
	cd services/usage-api && ruff check app tests
	cd services/notification-service && ruff check app tests

helm-check:          ## helm lint + render with the AWS-shaped CI fixture
	helm lint charts/telecom-app --strict -f charts/telecom-app/values-dev.yaml -f .github/ci/helm-values-aws.yaml
	helm template telecom charts/telecom-app -n telecom-dev -f charts/telecom-app/values-dev.yaml -f .github/ci/helm-values-aws.yaml > /dev/null

tf-check:            ## terraform fmt + validate without a backend
	terraform fmt -check -recursive terraform
	cd terraform && terraform init -backend=false -input=false >/dev/null && terraform validate

ci-local: lint test helm-check tf-check  ## everything CI runs except the scanners and AWS steps
