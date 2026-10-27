# Robust Load Testing for Generative AI Applications

This directory provides a comprehensive load testing framework for your Generative AI application, leveraging the power of [Locust](http://locust.io), a leading open-source load testing tool.

## Local Load Testing

Follow these steps to execute load tests on your local machine:

**1. Start the FastAPI Server:**

Launch the FastAPI server in a separate terminal:

```bash
uv run uvicorn {{cookiecutter.agent_package}}.fast_api_app:app --host 0.0.0.0 --port 8000 --reload
```

**2. (In another tab) Create virtual environment with Locust**
Using another terminal tab, This is suggested to avoid conflicts with the existing application python environment.

```bash
python3 -m venv .locust_env && source .locust_env/bin/activate && pip install locust==2.31.1
```

**3. Execute the Load Test:**
Trigger the Locust load test with the following command:

```bash
locust -f tests/load_test/load_test.py \
-H http://127.0.0.1:8000 \
--headless \
-t 30s -u 10 -r 2 \
--csv=tests/load_test/.results/results \
--html=tests/load_test/.results/report.html
```

This command initiates a 30-second load test, simulating 2 users spawning per second, reaching a maximum of 60 concurrent users.

**Results:**

Comprehensive CSV and HTML reports detailing the load test performance will be generated and saved in the `tests/load_test/.results` directory.

## Remote Load Testing (Targeting GKE)

This framework also supports load testing against a deployed GKE service. The GKE service uses an internal LoadBalancer, so you need `kubectl port-forward` to access it.

**Prerequisites:**

- **Dependencies:** Ensure your environment has the same dependencies required for local testing.
- **GKE Credentials:** You need `kubectl` configured with cluster credentials.

**Steps:**

**1. Get cluster credentials and start port-forward:**

```bash
gcloud container clusters get-credentials CLUSTER_NAME --region REGION --project PROJECT_ID
kubectl port-forward svc/SERVICE_NAME 8080:8080 -n NAMESPACE &
```

**2. (In another tab) Create virtual environment with Locust:**
```bash
python3 -m venv .locust_env && source .locust_env/bin/activate && pip install locust==2.31.1
```

**3. Execute the Load Test:**

```bash
locust -f tests/load_test/load_test.py \
-H http://127.0.0.1:8080 \
--headless \
-t 30s -u 10 -r 2 \
--csv=tests/load_test/.results/results \
--html=tests/load_test/.results/report.html
```
